"""Build script for the StreamBridge third-party Kodi repository (`dist/repo/`).

Follows official Kodi repository best practices:
- Packages each add-on in `addons/` into `dist/repo/<addon_id>/<addon_id>-<version>.zip`
- Generates a detached `<addon_id>-<version>.zip.sha256` file for `<hashes>sha256</hashes>` verification
- Copies each add-on's `addon.xml` into `dist/repo/<addon_id>/addon.xml`
- Combines all `<addon>` elements into `dist/repo/addons.xml` and writes `dist/repo/addons.xml.md5`
- Outputs a root-level `dist/repository.streambridge.zip` for one-step "Install from zip file" in Kodi
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
import zipfile

ROOT_DIR = Path(__file__).resolve().parents[1]
ADDONS_DIR = ROOT_DIR / "addons"
DIST_DIR = ROOT_DIR / "dist"
REPO_OUT_DIR = DIST_DIR / "repo"

EXCLUDED_NAMES = {"__pycache__", ".pytest_cache", ".DS_Store", "Thumbs.db"}


def should_skip(path: Path) -> bool:
    for part in path.parts:
        if part in EXCLUDED_NAMES or part.endswith((".pyc", ".pyo", ".tmp")):
            return True
    return False


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_repository() -> dict[str, str]:
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    REPO_OUT_DIR.mkdir(parents=True, exist_ok=True)

    addons_root = ET.Element("addons")
    packaged: dict[str, str] = {}

    for addon_dir in sorted(ADDONS_DIR.iterdir()):
        if not addon_dir.is_dir():
            continue
        addon_xml_path = addon_dir / "addon.xml"
        if not addon_xml_path.exists():
            continue

        tree = ET.parse(addon_xml_path)
        root = tree.getroot()
        addon_id = root.attrib["id"]
        version = root.attrib["version"]

        addons_root.append(root)

        target_dir = REPO_OUT_DIR / addon_id
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(addon_xml_path, target_dir / "addon.xml")

        zip_filename = f"{addon_id}-{version}.zip"
        zip_path = target_dir / zip_filename

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(addon_dir.rglob("*")):
                if file_path.is_dir() or should_skip(file_path):
                    continue
                rel_in_addon = file_path.relative_to(addon_dir)
                arcname = Path(addon_id) / rel_in_addon
                zf.write(file_path, arcname=str(arcname).replace("\\", "/"))

        digest_sha256 = sha256_file(zip_path)
        (target_dir / f"{zip_filename}.sha256").write_text(digest_sha256, encoding="utf-8")

        if addon_id == "repository.streambridge":
            shutil.copy2(zip_path, DIST_DIR / "repository.streambridge.zip")

        packaged[addon_id] = version

    ET.indent(addons_root, space="    ")
    addons_xml_str = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        + ET.tostring(addons_root, encoding="unicode")
        + "\n"
    )
    addons_xml_bytes = addons_xml_str.encode("utf-8")

    addons_xml_file = REPO_OUT_DIR / "addons.xml"
    addons_xml_file.write_bytes(addons_xml_bytes)

    md5_hex = hashlib.md5(addons_xml_bytes).hexdigest()
    (REPO_OUT_DIR / "addons.xml.md5").write_text(md5_hex, encoding="utf-8")

    return packaged


if __name__ == "__main__":
    built = build_repository()
    for k, v in built.items():
        print(f"Packaged {k} v{v}")
