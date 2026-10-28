"""``google-photos-picker login | pick`` – import photos from Google Photos in one command."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from .auth import OAuthCredentials, OAuthFlow
from .client import PickerClient
from .errors import PickerError
from .models import SCOPE, PickedMediaItem, PickingSession

DEFAULT_TOKEN = Path(os.environ.get(
    "GOOGLE_PHOTOS_PICKER_TOKEN",
    Path.home() / ".config" / "google-photos-picker" / "token.json"))


def print_qr(url: str) -> bool:
    """Print a terminal QR code if the optional ``qrcode`` package is installed."""
    try:
        import qrcode  # type: ignore
    except ImportError:
        return False
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(out=sys.stderr, invert=True)
    return True


def cmd_login(args: argparse.Namespace) -> None:
    cid = args.client_id or os.environ.get("GOOGLE_PHOTOS_PICKER_CLIENT_ID")
    secret = args.client_secret or os.environ.get("GOOGLE_PHOTOS_PICKER_CLIENT_SECRET")
    if not cid:
        sys.exit("--client-id (or GOOGLE_PHOTOS_PICKER_CLIENT_ID) is required")
    flow = OAuthFlow(cid, secret, [SCOPE])
    creds = flow.run_local_server(port=args.port, open_browser=not args.no_browser,
                                  print_fn=lambda m: print(m, file=sys.stderr))
    creds.save(args.token)
    print(f"Saved token to {args.token}", file=sys.stderr)


def cmd_pick(args: argparse.Namespace) -> None:
    token = Path(args.token)
    if not token.exists():
        sys.exit(f"No token at {token}. Run `google-photos-picker login` first.")

    def show(s: PickingSession) -> None:
        print(f"\nOpen this link and pick your photos:\n\n  {s.picker_uri}\n", file=sys.stderr)
        if args.qr and not print_qr(s.picker_uri):
            print("(install `qrcode` for a scannable QR code)", file=sys.stderr)
        print("Waiting for you to press Done in Google Photos...", file=sys.stderr)

    def on_item(item: PickedMediaItem, path: Optional[Path]) -> None:
        if path is None:
            print(f"  skipped {item.filename} (video still processing)", file=sys.stderr)
        else:
            print(path)

    with PickerClient(OAuthCredentials.load(token)) as client:
        paths = client.pick(args.out, max_items=args.max_items, show=show,
                            timeout=args.timeout, on_item=on_item, photo_size=args.max_size)
    print(f"Downloaded {len(paths)} file(s) to {args.out}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="google-photos-picker", description=__doc__)
    p.add_argument("--token", default=str(DEFAULT_TOKEN), help="token file (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="authorize via browser and save a token")
    s.add_argument("--client-id")
    s.add_argument("--client-secret")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("pick", help="pick photos in Google Photos and download them")
    s.add_argument("--out", default="google-photos", help="output directory")
    s.add_argument("--max-items", type=int, help="limit how many items can be picked (≤2000)")
    s.add_argument("--max-size", type=int,
                   help="resize photos to fit N×N pixels (videos stay full quality)")
    s.add_argument("--timeout", type=float, help="give up after N seconds")
    s.add_argument("--qr", action="store_true", help="also print a QR code (phone handoff)")
    s.set_defaults(func=cmd_pick)
    return p


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except PickerError as exc:
        sys.exit(f"error: {exc}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
