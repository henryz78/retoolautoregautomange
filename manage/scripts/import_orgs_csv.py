import argparse
import csv
import json
from pathlib import Path


def load_gateway_config(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_gateway_config(path: Path, data):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def resolve_org_target_path(gateway_config_path: Path, gateway_config: dict) -> Path | None:
    raw_path = gateway_config.get("orgs_file")
    if not raw_path:
        return None
    orgs_path = Path(str(raw_path))
    if not orgs_path.is_absolute():
        orgs_path = (gateway_config_path.parent / orgs_path).resolve()
    return orgs_path


def normalize_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def import_orgs(csv_path: Path, gateway_config_path: Path, replace: bool):
    gateway_config = load_gateway_config(gateway_config_path)
    org_target_path = resolve_org_target_path(gateway_config_path, gateway_config)

    if org_target_path:
        if org_target_path.exists():
            existing_orgs = load_gateway_config(org_target_path)
        else:
            existing_orgs = []
    else:
        existing_orgs = gateway_config.get("orgs", [])

    by_id = {org.get("id") or org["domain_name"]: org for org in existing_orgs}

    imported = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"id", "domain_name", "x_xsrf_token", "accessToken", "enabled"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"CSV missing required columns: {', '.join(sorted(missing))}")

        for row in reader:
            org = {
                "id": row["id"].strip(),
                "domain_name": row["domain_name"].strip(),
                "x_xsrf_token": row["x_xsrf_token"].strip(),
                "accessToken": row["accessToken"].strip(),
                "enabled": normalize_bool(row["enabled"]),
            }
            if not org["id"] or not org["domain_name"] or not org["x_xsrf_token"] or not org["accessToken"]:
                raise SystemExit(f"Invalid row with empty required values: {row}")
            imported.append(org)

    if replace:
        merged_orgs = imported
    else:
        for org in imported:
            by_id[org["id"]] = org
        merged_orgs = list(by_id.values())

    if org_target_path:
        write_gateway_config(org_target_path, merged_orgs)
        print(f"Imported {len(imported)} orgs into {org_target_path}")
        print(f"Org credential file now contains {len(merged_orgs)} orgs")
        return

    gateway_config["orgs"] = merged_orgs
    write_gateway_config(gateway_config_path, gateway_config)
    print(f"Imported {len(imported)} orgs into {gateway_config_path}")
    print(f"Gateway config now contains {len(gateway_config['orgs'])} orgs")


def main():
    parser = argparse.ArgumentParser(description="Import Retool org credentials from CSV into gateway_config.json")
    parser.add_argument("--csv", required=True, help="Path to org CSV file")
    parser.add_argument(
        "--gateway-config",
        default="gateway_config.json",
        help="Path to gateway_config.json",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing org list instead of merging by id",
    )
    args = parser.parse_args()

    import_orgs(Path(args.csv), Path(args.gateway_config), args.replace)


if __name__ == "__main__":
    main()
