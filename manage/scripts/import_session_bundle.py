import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from config import load_gateway_config, resolve_relative_path
from session_bundle import (
    SessionBundleError,
    load_session_bundle,
    session_bundle_to_org_records,
    update_import_history,
    write_json_file,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import a Windows-collected session_bundle.json into the Linux service org pool",
    )
    parser.add_argument(
        "--bundle",
        required=True,
        help="Path to session_bundle.json",
    )
    parser.add_argument(
        "--gateway-config",
        default="gateway_config.json",
        help="Path to gateway_config.json",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional explicit output path for managed org pool JSON. Defaults to gateway_config.orgs_file",
    )
    parser.add_argument(
        "--allow-expired",
        action="store_true",
        help="Allow importing expired orgs from the bundle",
    )
    parser.add_argument(
        "--history-output",
        default="runtime/session_bundle_imports.json",
        help="Import history ledger path",
    )
    return parser.parse_args()


def resolve_output_path(args: argparse.Namespace) -> Path:
    if args.output:
        return Path(args.output).resolve()

    gateway_config_path = Path(args.gateway_config).resolve()
    config = load_gateway_config(gateway_config_path)
    if not config.orgs_file:
        raise SystemExit("gateway_config.json 未配置 orgs_file，请显式传 --output")

    orgs_path = resolve_relative_path(gateway_config_path, config.orgs_file)
    if not orgs_path:
        raise SystemExit("无法解析 orgs_file 路径")
    return orgs_path


def main() -> None:
    args = parse_args()
    bundle_path = Path(args.bundle).resolve()
    output_path = resolve_output_path(args)
    history_output_path = Path(args.history_output).resolve()

    try:
        bundle = load_session_bundle(bundle_path)
        imported_orgs, skipped_orgs = session_bundle_to_org_records(
            bundle,
            allow_expired=args.allow_expired,
        )
    except SessionBundleError as exc:
        raise SystemExit(str(exc)) from exc

    if not imported_orgs:
        raise SystemExit("No importable org sessions found in the bundle")

    write_json_file(output_path, imported_orgs)
    update_import_history(
        history_output_path,
        {
            "bundle_path": str(bundle_path),
            "bundle_version": bundle.bundle_version,
            "generated_at": bundle.generated_at,
            "expires_at": bundle.expires_at,
            "imported_org_count": len(imported_orgs),
            "skipped_org_count": len(skipped_orgs),
            "output_path": str(output_path),
            "allow_expired": bool(args.allow_expired),
        },
    )

    print(f"Imported org sessions: {len(imported_orgs)}")
    print(f"Skipped org sessions: {len(skipped_orgs)}")
    print(f"Wrote managed org pool: {output_path}")
    print(f"Wrote import history: {history_output_path}")


if __name__ == "__main__":
    main()
