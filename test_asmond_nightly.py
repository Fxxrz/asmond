"""Nightly channel boundaries and isolated real installer/package integration."""
import contextlib
import io
import itertools
import json
import os
import re
import shlex
import shutil
from pathlib import Path
import subprocess
import tarfile
from unittest import mock

import test_asmond as fixtures


class NightlyInstallerTests(fixtures.IsolatedTestCase):
    local_processes = True
    def setUp(self):
        fixtures.DirectInstallerTests.setUp(self)
        self.home = self.base / "home"
        self.home.mkdir()
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)

    package = fixtures.DirectInstallerTests.package
    arguments = fixtures.DirectInstallerTests.arguments
    install = fixtures.DirectInstallerTests.install

    @classmethod
    def setUpClass(cls):
        fixtures.DirectInstallerTests.setUpClass.__func__(cls)

    def dev_source(self):
        source = self.base / "nightly-source"
        if not source.exists():
            source.mkdir()
            for name in self.installer.PACKAGE_FILES:
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.root / name, target)
            current = self.source_version
            development = current.split("-", 1)[0] + "-dev"
            for name in ("asmond.py", "man/asmond.1", "README.md"):
                path = source / name
                text = path.read_text().replace(current, development)
                if name == "README.md":
                    text = re.sub(r"^(?:Nightly build|Development build|Development checkout):.*$", "Development checkout: " + development + ".", text, flags=re.M)
                    if "Development checkout: " + development + "." not in text:
                        text += "\nDevelopment checkout: " + development + ".\n"
                path.write_text(text)
        return source

    def nightly(self, identity="20261122120000.g976bacb3487f"):
        archive, checksum = self.builder.build_archive(self.dev_source(), self.base / identity, nightly=identity)
        return archive, checksum.read_text().split()[0], archive.name[7:-7]

    @staticmethod
    def metadata(version, *, prerelease=True, draft=False):
        name = f"asmond-{version}.tar.gz"
        base = f"https://github.com/Fxxrz/asmond/releases/download/v{version}/"
        return {"tag_name": "v" + version, "prerelease": prerelease, "draft": draft,
                "assets": [{"name": asset, "browser_download_url": base + asset} for asset in (name, name + ".sha256")]}

    def test_versions_have_valid_dates_exact_identity_and_chronological_order(self):
        one = "1.1.0-dev.20261122120000.g976bacb3487f"
        two = "1.1.0-dev.20261122120001.g000000000001"
        self.assertLess(self.installer.version_tuple(one, development=True), self.installer.version_tuple(two, development=True))
        self.assertEqual(self.installer.version_channel(one), "nightly")
        self.assertEqual(self.installer.version_channel("1.1.0-dev"), "stable")
        for value in (one, "1.1.0-dev"):
            with self.assertRaises(self.installer.InstallError):
                self.installer.require_channel_version(value, "stable")
        for value in ("1.1.0", "1.1.0-dev", one.replace("20261122", "20260230"), one + "\n", one + "/x", one.replace(".g", ".G"), one.replace("976bacb3487f", "123")):
            with self.subTest(value=value), self.assertRaises(self.installer.InstallError):
                self.installer.require_channel_version(value, "nightly")
        with self.assertRaises(self.installer.InstallError):
            self.installer.require_channel_version("1.1.0", "other")

    def test_nightly_search_is_order_independent_paged_and_ignores_other_channels(self):
        first = "1.1.0-dev.20261122120000.g976bacb3487f"
        newer = "1.1.0-dev.20261122120001.g000000000001"
        ignored = self.metadata("9.0.0", prerelease=False)
        pages = [[ignored] * 99 + [self.metadata(newer)],
                 [self.metadata(first), self.metadata("9.0.0-dev.20261122120002.g123456789abc", draft=True), self.metadata("1.1.0-beta")]]
        def download(url, destination, limit):
            self.assertIn("per_page=100&page=", url)
            self.assertEqual(limit, 2 * 1024**2)
            destination.write_text(json.dumps(pages.pop(0)))
        with mock.patch.object(self.installer, "curl_download", side_effect=download) as request:
            self.assertEqual(self.installer.latest_nightly(self.base)["tag_name"], "v" + newer)
            self.assertEqual(request.call_count, 2)

    def test_search_failure_limit_and_ambiguous_identity_never_fall_back_to_stable(self):
        version = "1.1.0-dev.20261122120000.g976bacb3487f"
        for rows in ([], {}, [self.metadata("1.1.0", prerelease=False)],
                     [self.metadata(version), self.metadata(version.replace("976bacb3487f", "000000000001"))], [None] * 101):
            with self.subTest(rows=len(rows)), mock.patch.object(self.installer, "curl_download", side_effect=lambda _url, path, _limit: path.write_text(json.dumps(rows))):
                with self.assertRaises(self.installer.InstallError):
                    self.installer.latest_nightly(self.base)
        with mock.patch.object(self.installer, "curl_download", side_effect=lambda _url, path, _limit: path.write_text(json.dumps([None] * 100))) as request:
            with self.assertRaisesRegex(self.installer.InstallError, "search limit"):
                self.installer.latest_nightly(self.base)
            self.assertEqual(request.call_count, 10)

    def test_old_timestamp_collisions_never_mask_a_unique_newest_build(self):
        older = "1.1.0-dev.20261121120000.g111111111111"
        other = older.replace("111111111111", "222222222222")
        newest = "1.1.0-dev.20261122120000.g333333333333"
        for order in itertools.permutations((older, other, newest)):
            with self.subTest(order=order):
                rows = [self.metadata(version) for version in order]
                with mock.patch.object(self.installer, "curl_download", side_effect=lambda _url, path, _limit: path.write_text(json.dumps(rows))):
                    self.assertEqual(self.installer.latest_nightly(self.base)["tag_name"], "v" + newest)

    def test_newest_page_can_resolve_a_prior_page_timestamp_collision(self):
        older = "1.1.0-dev.20261121120000.g111111111111"
        other = older.replace("111111111111", "222222222222")
        newest = "1.1.0-dev.20261122120000.g333333333333"
        pages = [[self.metadata(older), self.metadata(other)] + [None] * 98, [self.metadata(newest)]]
        def download(_url, path, _limit):
            path.write_text(json.dumps(pages.pop(0)))
        with mock.patch.object(self.installer, "curl_download", side_effect=download) as reader:
            self.assertEqual(self.installer.latest_nightly(self.base)["tag_name"], "v" + newest)
            self.assertEqual(reader.call_count, 2)

    def test_actual_newest_collision_is_rejected_in_any_order_and_across_pages(self):
        older = "1.1.0-dev.20261121120000.g111111111111"
        latest = "1.1.0-dev.20261122120000.g222222222222"
        other = latest.replace("222222222222", "333333333333")
        for order in itertools.permutations((older, latest, other)):
            with self.subTest(order=order):
                rows = [self.metadata(version) for version in order]
                with mock.patch.object(self.installer, "curl_download", side_effect=lambda _url, path, _limit: path.write_text(json.dumps(rows))):
                    with self.assertRaisesRegex(self.installer.InstallError, "Ambiguous nightly"):
                        self.installer.latest_nightly(self.base)
        pages = [[self.metadata(latest)] + [None] * 99, [self.metadata(other)]]
        with mock.patch.object(self.installer, "curl_download", side_effect=lambda _url, path, _limit: path.write_text(json.dumps(pages.pop(0)))):
            with self.assertRaisesRegex(self.installer.InstallError, "Ambiguous nightly"):
                self.installer.latest_nightly(self.base)

    def test_duplicate_release_across_pages_still_identifies_one_build(self):
        latest = "1.1.0-dev.20261122120000.g222222222222"
        pages = [[self.metadata(latest)] + [None] * 99, [self.metadata(latest)]]
        with mock.patch.object(self.installer, "curl_download", side_effect=lambda _url, path, _limit: path.write_text(json.dumps(pages.pop(0)))):
            self.assertEqual(self.installer.latest_nightly(self.base)["tag_name"], "v" + latest)
        other = latest.replace("222222222222", "333333333333")
        rows = [self.metadata(latest), self.metadata(other), self.metadata(latest)]
        with mock.patch.object(self.installer, "curl_download", side_effect=lambda _url, path, _limit: path.write_text(json.dumps(rows))):
            with self.assertRaisesRegex(self.installer.InstallError, "Ambiguous nightly"):
                self.installer.latest_nightly(self.base)

    def test_remote_nightly_keeps_exact_assets_checksum_and_explicit_opt_in(self):
        archive, digest, version = self.nightly()
        meta = self.metadata(version)
        name = archive.name
        def download(url, path, _limit):
            if "/releases/tags/" in url:
                path.write_text(json.dumps(meta))
            elif "/releases?" in url:
                path.write_text(json.dumps([meta]))
            elif url.endswith(".sha256"):
                path.write_text(f"{digest}  {name}\n")
            else:
                path.write_bytes(archive.read_bytes())
        for pinned in (None, version):
            with mock.patch.object(self.installer, "curl_download", side_effect=download) as request:
                received = self.installer.release_download(pinned, self.base, channel="nightly")
                self.assertEqual((received[0], received[2]), (version, digest))
                self.assertTrue(all(call.args[0].startswith("https://") for call in request.call_args_list))
        with mock.patch.object(self.installer, "curl_download") as request:
            with self.assertRaises(self.installer.InstallError):
                self.installer.release_download(version, self.base)
            request.assert_not_called()
        for change in ("stable", "draft", "duplicate", "url", "tag"):
            bad = self.metadata(version)
            if change == "stable":
                bad["prerelease"] = False
            if change == "draft":
                bad["draft"] = True
            if change == "duplicate":
                bad["assets"].append(bad["assets"][0])
            if change == "url":
                bad["assets"][0]["browser_download_url"] = "https://example.com/archive.tar.gz"
            if change == "tag":
                bad["tag_name"] = "v" + version.replace("120000", "120001")
            with self.subTest(change=change), mock.patch.object(self.installer, "curl_download", side_effect=lambda _url, path, _limit: path.write_text(json.dumps(bad))):
                with self.assertRaises(self.installer.InstallError):
                    self.installer.release_download(version, self.base, channel="nightly")

    def test_builder_changes_only_payload_version_labels_and_is_reproducible(self):
        before = {name: (self.dev_source() / name).read_bytes() for name in self.installer.PACKAGE_FILES}
        package = self.nightly()
        second, _ = self.builder.build_archive(self.dev_source(), self.base / "repeated", nightly="20261122120000.g976bacb3487f")
        self.assertEqual(package[0].read_bytes(), second.read_bytes())
        self.assertTrue(all((self.dev_source() / name).read_bytes() == data for name, data in before.items()))
        with tarfile.open(package[0]) as archive:
            self.assertTrue(all(item.isfile() and item.uid == item.gid == item.mtime == 0 for item in archive))
            runtime = archive.extractfile(f"asmond-{package[2]}/asmond.py").read().decode()
            self.assertIn(f'VERSION = "{package[2]}"', runtime)
            manual = archive.extractfile(f"asmond-{package[2]}/man/asmond.1").read().decode()
            self.assertIn(f'"Asmond {package[2]}"', manual.splitlines()[0])
            self.assertIn("Nightly build: " + package[2], archive.extractfile(f"asmond-{package[2]}/README.md").read().decode())
        for identity in ("20260230120000.g976bacb3487f", "20261122120000.g123", "20261122120000.g976BACB3487F", "../../escape"):
            output = self.base / "invalid"
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.builder.build_archive(self.dev_source(), output, nightly=identity)
            self.assertFalse(output.exists())
        with self.assertRaises(ValueError):
            self.builder.build_archive(self.dev_source(), self.base / "invalid-combo", nightly="20261122120000.g976bacb3487f", allow_dev=True)

    def test_pinned_formula_uses_real_immutable_asset_digest_and_separate_command(self):
        archive, digest, version = self.nightly()
        checksum = Path(str(archive) + ".sha256")
        destination = self.base / "asmond-nightly.rb"
        self.builder.write_nightly_formula(archive, checksum, destination)
        text = destination.read_text()
        self.assertIn(f'/releases/download/v{version}/{archive.name}', text)
        self.assertIn(f'sha256 "{digest}"', text)
        self.assertIn(f'version "{version}"', text)
        self.assertIn('bin/"asmond-nightly"', text)
        self.assertNotIn('bin/"asmond"', text)
        self.assertIn('"asmond-nightly.1"', text)
        self.assertIn('Application Support/Asmond Nightly', text)
        self.assertNotIn('@VERSION@', text)
        self.assertNotIn(str(self.root), text)
        with self.assertRaises(FileExistsError):
            self.builder.write_nightly_formula(archive, checksum, destination)
        checksum.write_text("0" * 64 + "  " + archive.name + "\n")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.builder.write_nightly_formula(archive, checksum, self.base / "bad.rb")
        self.assertFalse((self.base / "bad.rb").exists())

    def test_stable_and_nightly_install_side_by_side_without_touching_stable(self):
        stable = self.package("1.1.0")
        self.install(self.arguments(stable))
        stable_files = {path.relative_to(self.prefix): path.read_bytes() for path in self.prefix.rglob("*") if path.is_file() and not path.is_symlink()}
        stable_launcher = (self.bin_dir / "asmond").read_bytes()
        nightly = self.nightly()
        night_prefix = self.base / "Nightly"
        self.install(self.arguments(nightly, prefix=str(night_prefix), channel="nightly"))
        config, version = self.installer.read_installation(night_prefix)
        self.assertEqual(config["channel"], "nightly")
        self.assertEqual(version, nightly[2])
        self.assertEqual((self.bin_dir / "asmond").read_bytes(), stable_launcher)
        self.assertTrue(all((self.prefix / name).read_bytes() == content for name, content in stable_files.items()))
        env = dict(os.environ, HOME=str(self.base / "home"))
        env.pop("ASMOND_SETTINGS_DIR", None)
        command = ["/bin/bash", str(self.bin_dir / "asmond-nightly")]
        for extra, expected in ((["--version"], f"Asmond {nightly[2]}"), (["--config-path"], str(Path(env["HOME"]) / "Library/Application Support/Asmond Nightly/settings.json"))):
            result = subprocess.run(command + extra, capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)
        env["ASMOND_SETTINGS_DIR"] = str(self.base / "explicit settings")
        result = subprocess.run(command + ["--config-path"], capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(result.stdout.strip(), str(self.base / "explicit settings/settings.json"))

    def test_nightly_update_retains_channel_rollback_verifies_and_failed_update_preserves_current(self):
        first = self.nightly()
        self.install(self.arguments(first, channel="nightly"))
        second = self.nightly("20261122120001.g000000000001")
        update = self.arguments(second, archive=None, sha256=None, version=None, update=True)
        with mock.patch.object(self.installer, "release_download", return_value=(second[2], second[0], second[1])) as download:
            self.install(update)
            self.assertEqual(download.call_args.kwargs, {"channel": "nightly"})
        self.assertEqual(self.installer.read_installation(self.prefix)[1], second[2])
        rollback = self.arguments(first, archive=None, sha256=None, version=None, rollback=True)
        self.install(rollback)
        self.assertEqual(self.installer.read_installation(self.prefix)[1], first[2])
        self.install(self.arguments(second))  # Same channel, retained verified payload.
        self.assertEqual(self.installer.read_installation(self.prefix)[1], second[2])
        third = self.nightly("20261122120002.g000000000002")
        with self.assertRaises(self.installer.InstallError):
            self.install(self.arguments(third, sha256="0" * 64))
        self.assertEqual(self.installer.read_installation(self.prefix)[1], second[2])
        with self.assertRaises(self.installer.InstallError):
            self.install(self.arguments(third, channel="stable"))
        self.assertEqual(self.installer.read_installation(self.prefix)[0]["channel"], "nightly")

    def test_documented_default_nightly_rollback_leaves_stable_untouched(self):
        stable_prefix = self.home / ".local/share/asmond"
        nightly_prefix = self.home / ".local/share/asmond-nightly"
        for version in ("1.0.0", "1.1.0"):
            self.install(self.arguments(self.package(version), prefix=str(stable_prefix)))
        first = self.nightly()
        second = self.nightly("20261122120001.g000000000001")
        for package in (first, second):
            self.install(self.arguments(package, prefix=str(nightly_prefix), channel="nightly"))
        stable_files = {p.relative_to(stable_prefix): p.read_bytes() for p in stable_prefix.rglob("*") if p.is_file()}
        guide = (self.root / "docs/direct-install.md").read_text()
        command = next(line for line in guide.splitlines() if line.startswith("bash ~/.local/share/asmond-nightly/"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.installer.main(shlex.split(command)[2:] + ["--yes"]), 0)
        self.assertEqual(self.installer.read_installation(nightly_prefix)[1], first[2])
        self.assertEqual(self.installer.read_installation(stable_prefix)[1], "1.1.0")
        self.assertTrue(all((stable_prefix / name).read_bytes() == data for name, data in stable_files.items()))

    def test_channel_mismatch_and_corrupt_retained_metadata_are_rejected_before_smoke(self):
        first = self.nightly()
        self.install(self.arguments(first, channel="nightly"))
        with mock.patch.object(self.installer, "smoke_test") as smoke:
            with self.assertRaises(self.installer.InstallError):
                self.install(self.arguments(self.package("1.1.0"), channel="stable"))
            smoke.assert_not_called()
        second = self.nightly("20261122120001.g000000000001")
        self.install(self.arguments(second))
        metadata_path = self.prefix / "releases" / first[2] / ".asmond-release.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["channel"] = "stable"
        metadata_path.write_text(json.dumps(metadata))
        with mock.patch.object(self.installer, "smoke_test") as smoke:
            with self.assertRaises(self.installer.InstallError):
                self.install(self.arguments(first, archive=None, sha256=None, version=None, rollback=True))
            smoke.assert_not_called()
        self.assertEqual(self.installer.read_installation(self.prefix)[1], second[2])

    def test_old_stable_installation_metadata_remains_compatible(self):
        package = self.package("1.1.0")
        self.install(self.arguments(package))
        for path in (self.prefix / ".asmond-install.json", self.prefix / "releases" / package[2] / ".asmond-release.json"):
            metadata = json.loads(path.read_text())
            metadata.pop("channel")
            path.write_text(json.dumps(metadata))
        config, version = self.installer.read_installation(self.prefix)
        self.assertNotIn("channel", config)
        self.assertEqual(version, "1.1.0")
        self.install(self.arguments(self.package("1.1.1"), update=True))
        self.assertEqual(self.installer.read_installation(self.prefix)[1], "1.1.1")
        self.assertFalse((self.bin_dir / "asmond-nightly").exists())

    def test_default_nightly_location_is_separate_and_missing_release_changes_nothing(self):
        package = self.nightly()
        home = self.home
        self.install(self.arguments(package, prefix=None, channel="nightly"))
        prefix = home / ".local/share/asmond-nightly"
        self.assertEqual(self.installer.read_installation(prefix)[1], package[2])
        self.assertFalse((home / ".local/share/asmond").exists())
        with mock.patch.object(self.installer, "release_download", side_effect=self.installer.InstallError("No published nightly")):
            target = self.base / "no-release"
            with self.assertRaises(self.installer.InstallError):
                self.install(self.arguments(package, prefix=str(target), bin_dir=str(self.base / "no-bin"), archive=None, sha256=None, version=None, channel="nightly"))
            self.assertFalse(target.exists())
            self.assertFalse((self.base / "no-bin").exists())

    def test_nightly_homebrew_update_guidance_never_calls_the_script_installer(self):
        with mock.patch.object(fixtures.asmond, "VERSION", "1.1.0-dev.20261122120000.g976bacb3487f"), mock.patch.object(fixtures.asmond, "__file__", "/tmp/Cellar/asmond-nightly/libexec/asmond.py"), mock.patch.object(fixtures.asmond, "print_console") as console, mock.patch.object(fixtures.asmond.subprocess, "call") as run:
            self.assertEqual(fixtures.asmond.run_self_update(), 1)
            self.assertIn("brew upgrade asmond-nightly", console.call_args.args[0])
            run.assert_not_called()

    def test_nightly_header_preserves_theme_layout_and_controls_in_all_themes(self):
        version = "1.1.0-dev.20261122120000.g976bacb3487f"
        with mock.patch.object(fixtures.asmond, "VERSION", version):
            fixtures.GraphSelectionStyleTests().test_header_highlights_active_theme_and_layout_with_bounded_controls()
        for width, label in ((72, "nightly"), (100, "nightly"), (181, version)):
            args = fixtures.LayoutTests().dashboard_args("full")
            window = fixtures.LayoutTests.RecordingWindow(3, width)
            with mock.patch.object(fixtures.asmond, "VERSION", version):
                fixtures.asmond.draw_header(window, args, fixtures.LayoutTests.dashboard_colors())
            self.assertIn(label, window.rendered_row(0))
