import glob
import os
import re
import subprocess
import tempfile

from gi.repository import GLib

from .. import appstream, builddir, ostree
from . import Check


class DesktopfileCheck(Check):
    def _validate(self, path: str, appid: str) -> None:
        appstream_path = f"{path}/app-info/xmls/{appid}.xml.gz"
        desktopfiles_path = f"{path}/applications"
        icon_path = f"{path}/icons/hicolor"
        glob_path = f"{icon_path}/*/apps/*"

        desktop_files = []
        if os.path.exists(desktopfiles_path):
            desktop_files = [
                file
                for file in os.listdir(desktopfiles_path)
                if os.path.isfile(f"{desktopfiles_path}/{file}")
                and re.match(rf"^{appid}([-.].*)?\.desktop$", file)
            ]

        if appid.endswith(".BaseApp"):
            return None

        if not os.path.exists(appstream_path):
            return None

        if len(appstream.components(appstream_path)) != 1:
            return None

        if appstream.component_type(appstream_path) not in (
            "desktop",
            "desktop-application",
        ):
            return None

        icon_list = []
        icon_files_list = []
        if os.path.exists(icon_path):
            icon_list = [
                file
                for file in glob.glob(glob_path)
                if re.match(rf"^{appid}([-.].*)?$", os.path.basename(file))
                and os.path.isfile(file)
            ]
            icon_files_list = [os.path.basename(i) for i in icon_list]
        if not len(icon_list) > 0:
            self.errors.add("no-exportable-icon-installed")
            self.info.add(
                "no-exportable-icon-installed: No PNG or SVG icons named by FLATPAK_ID"
                + " were found in /app/share/icons/hicolor/$size/apps"
                + " or /app/share/icons/hicolor/scalable/apps"
            )

        if not len(desktop_files) > 0:
            self.errors.add("desktop-file-not-installed")
            self.info.add(
                "desktop-file-not-installed: No desktop file matching FLATPAK_ID"
                + " was found in /app/share/applications"
            )

        for file in desktop_files:
            if os.path.exists(f"{desktopfiles_path}/{file}"):
                cmd = subprocess.run(
                    [
                        "desktop-file-validate",
                        "--no-hints",
                        "--no-warn-deprecated",
                        f"{desktopfiles_path}/{file}",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                if cmd.returncode != 0:
                    self.errors.add("desktop-file-failed-validation")
                    self.info.add(
                        f"desktop-file-failed-validation: Desktop file: {file}"
                        + " has failed validation. Please see the errors in desktopfile block"
                    )
                    for p in cmd.stdout.decode("utf-8").split(f"{file}:")[1:]:
                        self.desktopfile.add(p.strip())

        if os.path.exists(f"{desktopfiles_path}/{appid}.desktop"):
            key_file = GLib.KeyFile.new()
            key_file.load_from_file(
                f"{desktopfiles_path}/{appid}.desktop", GLib.KeyFileFlags.NONE
            )

            if key_file.get_start_group() != "Desktop Entry":
                raise GLib.Error("Unknown start group in desktop file.")

            try:
                icon = key_file.get_string("Desktop Entry", "Icon")
            except GLib.Error:
                icon = None

            if icon is None:
                self.errors.add("desktop-file-icon-key-absent")
            else:
                if not len(icon) > 0:
                    self.errors.add("desktop-file-icon-key-empty")
                if len(icon) > 0:
                    if not re.match(rf"^{appid}([-.].*)?$", f"{icon}"):
                        self.errors.add("desktop-file-icon-key-wrong-value")
                        self.info.add(
                            "desktop-file-icon-key-wrong-value: Icon key in desktop file has"
                            + f" wrong value: {icon}"
                        )
                    if icon_files_list:
                        found_icons = set(icon_files_list)
                        if not any(
                            k in icon_files_list
                            for k in (
                                icon,
                                icon + ".png",
                                icon + ".svg",
                                icon + ".svgz",
                            )
                        ):
                            self.errors.add("desktop-file-icon-not-installed")
                            self.info.add(
                                "desktop-file-icon-not-installed: An icon named by value of"
                                + f" icon key in desktop file: {icon} was not found."
                                + f" Found icons: {found_icons}"
                            )

            try:
                exect = key_file.get_string("Desktop Entry", "Exec")
            except GLib.Error:
                exect = None

            if exect is None:
                # https://gitlab.freedesktop.org/xdg/desktop-file-utils/-/issues/6
                self.errors.add("desktop-file-exec-key-absent")
            else:
                if not len(exect) > 0:
                    # https://github.com/flatpak/flatpak/commit/298286be2d8ceacc426dedecc0e38a3f82d8aedc
                    # Flatpak allows exporting empty Exec key because it is
                    # going to be rewritten w/o command and command in
                    # manifest is going to be used as default. Rely on
                    # fallback but also warn
                    self.warnings.add("desktop-file-exec-key-empty")
                if len(exect) > 0 and "flatpak run" in exect:
                    # desktop files are rewritten only on (re)install, neither
                    # exported ref or builddir should have "flatpak run"
                    # unless manually added in desktop-file
                    # https://github.com/flatpak/flatpak/blob/65bc369a9f7851cc1344d2a767b308050cd66fe3/common/flatpak-transaction.c#L4765
                    # flatpak-dir.c: export_desktop_file < rewrite_export_dir
                    # < flatpak_rewrite_export_dir < flatpak_dir_deploy
                    # < flatpak_dir_deploy_install < flatpak_dir_install
                    self.errors.add("desktop-file-exec-has-flatpak-run")
                    self.info.add(
                        f"desktop-file-exec-has-flatpak-run: Exec key: {exect}"
                        + " uses flatpak run in it"
                    )

            try:
                hidden = key_file.get_boolean("Desktop Entry", "Hidden")
            except GLib.Error:
                hidden = None

            if hidden is True:
                self.errors.add("desktop-file-is-hidden")

            try:
                nodisplay = key_file.get_boolean("Desktop Entry", "NoDisplay")
            except GLib.Error:
                nodisplay = None

            if nodisplay is True:
                self.errors.add("desktop-file-is-hidden")

            try:
                cats = set(key_file.get_string_list("Desktop Entry", "Categories"))
            except GLib.Error:
                cats = None

            # https://github.com/ximion/appstream/blob/a98d98ec1b75d7e9402d8d103802992415075d2f/src/as-utils.c#L1337-L1364
            # appstreamcli filters these out during compose, "GUI"
            # is caught by desktop-file-validate
            block = {
                "GTK",
                "Qt",
                "KDE",
                "GNOME",
                "Motif",
                "Java",
                "Application",
                "XFCE",
                "DDE",
            }
            if cats is not None and len(cats) > 0 and cats.issubset(block):
                found_cats = cats.intersection(block)
                self.warnings.add("desktop-file-low-quality-category")
                self.info.add(
                    "desktop-file-low-quality-category: A low quality category was found"
                    + f"in the desktop file: {found_cats}"
                )

    def check_build(self, path: str) -> None:
        appid = builddir.infer_appid(path)
        if not appid:
            return
        metadata = builddir.parse_metadata(path)
        if not metadata:
            return
        if metadata.get("type", False) != "application":
            return

        self._validate(f"{path}/files/share", appid)

    def check_repo(self, path: str) -> None:
        self._populate_ref(path)
        ref = self.repo_primary_ref
        if not ref:
            return
        appid = ref.split("/")[1]

        if appid.endswith(".BaseApp"):
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            retm = ostree.extract_subpath(path, ref, "/metadata", tmpdir)
            if retm["returncode"] != 0:
                raise RuntimeError("Failed to extract ostree repo")
            metadata = builddir.parse_metadata(tmpdir)
            if not metadata:
                return
            if metadata.get("type", False) != "application":
                return

            ret = ostree.extract_subpath(path, ref, "files/share", tmpdir)
            if ret["returncode"] != 0:
                raise RuntimeError("Failed to extract ostree repo")
            self._validate(tmpdir, appid)
