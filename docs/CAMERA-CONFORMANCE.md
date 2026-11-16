# Camera conformance

The CI smoke test uses real HTTP and a separate disposable simulator process:
connect, read Camera V3 identity and geometry, expose for 0.1 seconds, wait for
ImageReady with a deadline, retrieve and decode ImageBytes, compare JSON pixels,
abort a second exposure and disconnect. It verifies transaction IDs, binary
header/type/dimensions/length, image readiness and graceful SIGTERM cleanup.

```sh
python3 -m unittest discover -s tests -p test_alpaca_smoke.py -v
```

`scripts/alpaca_conformance_demo.py --demo` runs the production camera worker and
Alpaca HTTP listener with one 320×240 synthetic monochrome camera. The explicit
flag is required. It binds only `127.0.0.1`; `--port 0` chooses an unused port.
Its single stdout JSON line reports the actual endpoint. Camera state stays in
memory and SIGINT/SIGTERM closes the listener and worker. It performs no USB
probing, network-controller work, instrument access or UDP broadcast discovery.
This is a **camera-service demo**, separate from the full web application's
`python3 -m wifi_bridge --demo`, which still starts without simulated cameras.
The small image allows both JSON and ImageBytes conformance checks within the
existing 1 Mpx JSON limit. Full-size simulator and memory limits remain covered
by the camera unit tests.

## External checker status

The official [ASCOM Initiative ConformU v4.5.3 release](https://github.com/ASCOMInitiative/ConformU/releases/tag/v4.5.3)
was retrieved on 2026-11-04. Both downloaded assets matched the published GitHub
release-asset SHA-256 digests:

| Asset | SHA-256 |
| --- | --- |
| `ConformU-Installer.4.5.3.dmg` | `f08a2bfc2304f60a2fd9f2958f9d3052b0bd9df5e73934771f23f3c7d5a59ec9` |
| `conformu.4.5.3.linux-arm64.tar.gz` | `8c01fce9447f78e7f076c2279d6f38c7b8f78ef8d3c9f92e78566a61fb778bbc` |

The Mac DMG passed its internal CRC checks, but `hdiutil attach -readonly
-nobrowse` with a mountpoint on the external disk failed with `Permission denied`. Built-in
`bsdtar` does not recognize this DMG; no `7zz`, `7z`, `unar` or `dmg2img` was
available. The official Linux ARM64 alternative contains a bundled ELF executable
and no portable `conformu.dll`/runtime configuration. The installed Mac .NET
10.0.11 rejected `dotnet exec` on that bundle (exit 129). No OS security settings,
system applications or additional runtimes were changed.

**ConformU did not run against the camera.** Its errors and warnings are unknown;
there are no accepted deviations and no zero-error conformance claim. The smoke
test is not an external conformance certificate. Downloads, original release
metadata, extraction and the environment report remain on the local disk under
`$CONFORMANCE_DIR` (default `./conformance/`); they are not source
artifacts. Physical cameras, Raspberry Pi performance, LAN/hotspot discovery and
real client interoperability remain **documented, not verified on hardware**.

## Owner procedure on a Mac that can mount the official DMG

Run these commands from the repository checkout. They preserve the normal demo on
port 18787 and keep downloads, settings, logs and tool runtime state in the conformance directory.
If macOS still denies mounting or executing the official application, stop and
resolve the normal owner installation policy; do not disable OS protections.

```sh
conformance_dir="${CONFORMANCE_DIR:-$PWD/conformance}"
export CONFORMANCE_DIR="$conformance_dir"
mkdir -p "$conformance_dir/mount" "$conformance_dir/app" \
  "$conformance_dir/dotnet-home" "$conformance_dir/nuget" "$conformance_dir/bundle"
export DOTNET_CLI_HOME="$conformance_dir/dotnet-home"
export NUGET_PACKAGES="$conformance_dir/nuget"
export DOTNET_BUNDLE_EXTRACT_BASE_DIR="$conformance_dir/bundle"
export DOTNET_CLI_TELEMETRY_OPTOUT=1
curl -fL https://github.com/ASCOMInitiative/ConformU/releases/download/v4.5.3/ConformU-Installer.4.5.3.dmg \
  -o "$conformance_dir/ConformU-Installer.4.5.3.dmg" || exit 1
python3 - <<'PY' || exit 1
import hashlib, os
from pathlib import Path
p = Path(os.environ['CONFORMANCE_DIR']) / 'ConformU-Installer.4.5.3.dmg'
with p.open('rb') as stream:
    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
assert digest == 'f08a2bfc2304f60a2fd9f2958f9d3052b0bd9df5e73934771f23f3c7d5a59ec9'
PY
hdiutil attach "$conformance_dir/ConformU-Installer.4.5.3.dmg" \
  -readonly -nobrowse -mountpoint "$conformance_dir/mount" || exit 1
```

Copy only the app bundle to the conformance directory and resolve its declared executable, without
installing into `/Applications`:

```sh
conformu_executable=$(python3 - <<'PY'
import os, plistlib, subprocess
from pathlib import Path
root = Path(os.environ['CONFORMANCE_DIR'])
apps = list((root / 'mount').glob('*.app'))
assert len(apps) == 1, 'Inspect the official DMG app layout before proceeding'
target = root / 'app' / apps[0].name
subprocess.run(['/usr/bin/ditto', str(apps[0]), str(target)], check=True)
with (target / 'Contents/Info.plist').open('rb') as stream:
    name = plistlib.load(stream)['CFBundleExecutable']
executable = target / 'Contents/MacOS' / name
assert executable.is_file()
print(executable)
PY
) || exit 1
hdiutil detach "$conformance_dir/mount"
"$conformu_executable" conformance --help || exit 1
cat > "$conformance_dir/settings.json" <<'JSON'
{
  "SettingsCompatibilityVersion": 1,
  "RiskAcknowledged": true,
  "UpdateCheck": false,
  "Debug": false,
  "TraceDiscovery": false,
  "TraceAlpacaCalls": false,
  "CameraExposureDuration": 0.1
}
JSON
```

Start the camera-service demo in a second terminal, from the same checkout:

```sh
python3 scripts/alpaca_conformance_demo.py --demo --port 11112
```

Use the literal loopback endpoint printed by that process. In the first terminal,
run the full Camera check, keeping all mandatory tests enabled:

```sh
"$conformu_executable" conformance http://127.0.0.1:11112/api/v1/camera/0 \
  --settingsfile "$conformance_dir/settings.json" \
  --logfilename "$conformance_dir/camera-conformance.txt" \
  --resultsfile "$conformance_dir/camera-conformance.json" \
  > "$conformance_dir/camera-console.txt" 2>&1
conformance_status=$?
printf 'ConformU exit status: %s\n' "$conformance_status"
```

The CLI flags and versioned settings above come from the official
[v4.5.3 command definitions](https://github.com/ASCOMInitiative/ConformU/blob/v4.5.3/ConformU/Program.cs)
and [settings source](https://github.com/ASCOMInitiative/ConformU/blob/v4.5.3/ConformU/Settings.cs).
The full `conformance` command enables mandatory interface tests. Do not replace
it with a reduced settings run to hide failures. Check the saved JSON/text report,
fix failures, rerun and list every warning before claiming acceptance. Stop the
demo with Ctrl-C when finished. These owner steps have not been executed past
the blocked mount in this environment; the app's declared launcher/CLI must be
confirmed by its `conformance --help` output before running the final command.
