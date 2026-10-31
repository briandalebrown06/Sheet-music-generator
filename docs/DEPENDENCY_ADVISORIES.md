# Deployment dependency security review

Reviewed **2026-10-27**. **The current ML dependency stack has unresolved security advisories; public release remains gated on remediation or a documented security review accepting the specific residual risks.** The application audit, successful dependency resolution, and passing functional tests do not establish that these dependencies are safe for public deployment.

## Scope and method

The audit queried the public [OSV batch API](https://google.github.io/osv.dev/post-v1-querybatch/). The initial arm64 graph result flagged Keras 2.15.0, protobuf 4.25.9, setuptools 79.0.1, and Torch 2.11.0. We upgraded Torch to 2.14.1 and regenerated both architecture locks. The final scan covered **114 distinct package/version pairs** across both updated graphs (112 packages on amd64, 113 on arm64, including their different TensorFlow distributions). It returned findings for only Keras, protobuf, and setuptools: **16 GHSA records remain**, plus aliases. GHSA and PYSEC identifiers can describe the same issue; they are not additional independent vulnerabilities. This is a package-level scan, not a claim that every reported issue is reachable through this app.

We read current OSV records, relevant upstream advisories/documentation, package metadata on PyPI, and the application/model-loading source. No exploit against a deployed service was attempted. This review does not cover every potential native-library issue, the operating-system packages in an actual built image, or future advisories. The Linux images have not yet been built or run in this environment; cross-platform resolution and available wheels are documented in [DEPENDENCY_LOCKS.md](../deploy/DEPENDENCY_LOCKS.md).

## Findings and actual application exposure

| Locked package | Finding and published fix | Application path and disposition |
| --- | --- | --- |
| Keras 2.15.0 | Thirteen package-level advisories concerning model/config deserialization, HDF5, or archive handling; full list below. Fixes are in Keras 3.x. | Uploads are audio, never Keras models, archives, or model configuration. The Basic Pitch adapter selects the packaged ONNX model. However, upstream Basic Pitch imports TensorFlow when installed and its `Model` loader tries TensorFlow/TFLite before ONNX, so this is **not** a claim that the legacy runtime is absent or never initialized. The Linux dependency graph still installs Keras. No route from uploaded audio to the reported Keras model/archive entry points was identified. Exact applicability of every broadly versioned advisory to Keras 2.15 was not independently established. Remains flagged; do not suppress all Keras findings merely because ONNX is selected. |
| protobuf 4.25.9 | [GHSA-7gcm-g887-7qv7 / CVE-2026-0994](https://osv.dev/vulnerability/GHSA-7gcm-g887-7qv7): nested `Any` JSON bypasses the recursion limit in `json_format.ParseDict()`. OSV lists fixes 5.29.6 and 6.33.5. | The API accepts typed application JSON and decoded audio; it does not call `ParseDict` on user input. Model protobuf data is supplied by installed packages. No application path to the [reported JSON parser issue](https://github.com/protocolbuffers/protobuf/issues/25070) was identified. Still installed and flagged; upgrading within TensorFlow 2.15's declared constraint cannot reach a fixed version. |
| setuptools 79.0.1 | [GHSA-h35f-9h28-mq5c / CVE-2026-59890](https://osv.dev/vulnerability/GHSA-h35f-9h28-mq5c): Unicode normalization can bypass `MANIFEST.in` exclusions while producing an sdist on macOS. OSV lists 83.0.0 as fixed; the [upstream advisory](https://github.com/pypa/setuptools/security/advisories/GHSA-h35f-9h28-mq5c) still showed no patched version when checked. | The deployment uses Linux and hash-checked wheels; the service does not build or publish source distributions. Thus the described production build path is not used. Local maintainers must still avoid packaging private files and inspect release archives. Pin remains flagged. |
| setuptools 79.0.1 | [GHSA-xmj3-9gf7-h52w](https://github.com/pypa/setuptools/security/advisories/GHSA-xmj3-9gf7-h52w): Windows path escape via `namespace_packages.txt` in the legacy `Wheel.install_as_egg()` installer; fixed 84.1.0. | The target is Linux; installation uses pip with pinned wheel hashes, and runtime requests never install packages. The described Windows legacy installer path is not used. Pin remains flagged. |
| Torch **2.14.1**, updated from 2.11.0 | **Remediated package version:** [GHSA-rrmf-rvhw-rf47 / CVE-2025-3000](https://osv.dev/vulnerability/GHSA-rrmf-rvhw-rf47) concerns memory corruption through `torch.jit.script`; OSV lists 2.13.0 as fixed. The updated 2.14.1 scan returned no Torch advisories. | The separator runs the Open-Unmix network eagerly; no application or Open-Unmix adapter call to `torch.jit.script` was found. Only administrator-provisioned, checksum-verified weights are loaded, with `weights_only=True`. The 2.14.1 upgrade passed actual macOS inference regressions; Linux runtime qualification remains open. |

The code-path statements above are an inspection-based assessment, not proof of non-exploitability. Audio decoding, inference, and rendering still process untrusted input, and their other attack surfaces are not cleared by this table. Worker network isolation, process resource limits, private storage, and authorization reduce exposure and impact but do not patch vulnerable packages.

## Why an automatic version bump is insufficient

The [Basic Pitch 0.4.0 package metadata](https://pypi.org/pypi/basic-pitch/0.4.0/json) declares `tensorflow>=2.4.1,<2.15.1` on non-Darwin Python 3.11, even with the ONNX extra. The chosen [TensorFlow 2.15.0.post1 metadata](https://pypi.org/pypi/tensorflow/2.15.0.post1/json) requires `keras>=2.15.0,<2.16` and `protobuf<5.0.0dev`. These bounds exclude the published Keras and protobuf fixes. The [protobuf PyPI release list](https://pypi.org/pypi/protobuf/json) contained no 4.25.10 patch when checked; 4.25.9 was the newest 4.25 release.

Basic Pitch also requires `resampy>=0.2.2,<0.4.3`. Installed resampy 0.4.2 imports `pkg_resources` at runtime. Current setuptools releases have removed it: the checksum-verified official setuptools 84.1.0 wheel contains no `pkg_resources/` package. Consequently, simply bumping setuptools to the fixed version would break this inference dependency. Resampy 0.4.3 changes resource loading, but lies outside Basic Pitch's declared range. The [setuptools history](https://setuptools.pypa.io/en/latest/history.html) documents the `pkg_resources` retirement. Silently overriding metadata or installing with `--no-deps` is not an accepted fix.

Torch is different. [Current TorchAudio 2.11 documentation](https://docs.pytorch.org/audio/stable/installation.html) explicitly states that its stable ABI supports Torch 2.11 and newer. We installed Torch 2.14.1 in the actual macOS Python 3.11 environment, retained TorchAudio 2.11, ran `pip check`, and passed 38 targeted tests covering real UMX weight loading, alignment/repeatability, Basic Pitch ONNX upload-to-MIDI parsing, pYIN, and mixed-project persistence. The [official CPU wheel index](https://download.pytorch.org/whl/cpu/torch/) supplies 2.14.1 CPython 3.11 wheels for both Linux architectures; their metadata and hashes were checked, and both full deployment graphs resolved with binary wheels only. These Linux checks are not Linux inference results.

## Keras advisory inventory

These are the OSV records returned for Keras 2.15.0. Some descriptions discuss features introduced in Keras 3 while their machine-readable affected range starts at zero; a package-version hit is not independent evidence that the particular feature exists in 2.15. The application does not offer any of these model/archive loading interfaces to users.

| Advisory | Reported entry point | Fixed version(s) in OSV |
| --- | --- | --- |
| [GHSA-26c4-7vv6-867j](https://osv.dev/vulnerability/GHSA-26c4-7vv6-867j) | HDF5 virtual datasets; local file disclosure | 3.12.3 / 3.15.0 |
| [GHSA-36fq-jgmw-4r9c](https://osv.dev/vulnerability/GHSA-36fq-jgmw-4r9c) | Crafted model deserialization; code execution | 3.11.0 |
| [GHSA-4f3f-g24h-fr8m](https://osv.dev/vulnerability/GHSA-4f3f-g24h-fr8m) | TFSMLayer model deserialization | 3.13.2 |
| [GHSA-58hv-7753-xmfq](https://osv.dev/vulnerability/GHSA-58hv-7753-xmfq) | Tar symlink traversal | 3.12.3 / 3.15.0 |
| [GHSA-5gwj-m78q-7pq3](https://osv.dev/vulnerability/GHSA-5gwj-m78q-7pq3) | Lambda configuration deserialization | 3.12.3 / 3.15.0 |
| [GHSA-74m6-m3xx-3vmj](https://osv.dev/vulnerability/GHSA-74m6-m3xx-3vmj) | HDF5 shape allocation denial of service | 3.15.0 |
| [GHSA-cjgq-5qmw-rcj6](https://osv.dev/vulnerability/GHSA-cjgq-5qmw-rcj6) | Crafted tar path traversal | Record says last affected 3.7.0; no fixed event |
| [GHSA-gh82-f9x8-5frx](https://osv.dev/vulnerability/GHSA-gh82-f9x8-5frx) | DiskIOStore layer-name path traversal | 3.12.3 / 3.15.0 |
| [GHSA-hjqc-jx6g-rwp9](https://osv.dev/vulnerability/GHSA-hjqc-jx6g-rwp9) | `get_file` archive extraction | 3.12.0 |
| [GHSA-hqp4-2352-xf5r](https://osv.dev/vulnerability/GHSA-hqp4-2352-xf5r) | Archive destination validation | 3.14.0 |
| [GHSA-m8wh-29wm-52mv](https://osv.dev/vulnerability/GHSA-m8wh-29wm-52mv) | HDF5 external links; local file disclosure | 3.12.3 / 3.15.0 |
| [GHSA-mq84-hjqx-cwf2](https://osv.dev/vulnerability/GHSA-mq84-hjqx-cwf2) | StringLookup vocabulary path; file read/SSRF | 3.12.0 |
| [GHSA-v2w2-w228-c444](https://osv.dev/vulnerability/GHSA-v2w2-w228-c444) | TorchModuleWrapper unsafe pickle loading | 3.12.3 / 3.15.0 |

Where two fixes are listed, OSV describes separate affected release intervals; neither means that Keras 2.x has a backport.

## Required launch decision and verification

1. Remove the obsolete dependency constraints through a maintained upstream update or an explicitly maintained, licensed ONNX-only Basic Pitch package/adapter. Preserve the real model, outputs, offsets, note construction, and model provenance. Do not substitute fabricated notes. If a fork is necessary, record its source revision, packaging changes, review owner, and reproducible build; retain notices and verify code/model licenses separately.
2. Replace the `pkg_resources` dependency through supported resampy packaging or a reviewed upstream backport, then select a patched setuptools release. Select a compatible protobuf version that clears its advisory after the TensorFlow constraint is removed or updated. Do not pretend that a constraints-file override alone proves compatibility.
3. Qualify the patched Torch 2.14.1 runtime on Linux with actual UMX-HQ fixture separation, note transcription, cancellation, memory-limit, and worker-restart checks. The package update, macOS regressions, and both architecture locks are complete. Run `pip check` and rescan the exact installed distributions and final OS image after building.
4. Build and run the intended Linux image and the upload → process → edit → save → MusicXML/MIDI/PDF flow with offline model storage and the production proxy configuration. Compare fixture results before and after dependency changes. Record architecture, image digest, package versions, and actual results.
5. If remediation is deferred, a release owner must explicitly accept each remaining advisory with package/version, affected feature, evidence of current reachability, mitigations, expiration/review date, and a responsible maintainer. This document does **not** grant that acceptance. Reassess immediately if model uploads, custom model paths, JIT compilation, archive imports, or package installation become user-accessible.

The only runtime dependency changed for this remediation was Torch, from 2.11.0 to 2.14.1. The legacy TensorFlow, Keras, protobuf, resampy, and setuptools constraints were not overridden. The unresolved findings remain visible rather than being hidden by scanner exclusions. See the broader [deployment audit and launch checklist](DEPLOYMENT_AUDIT.md).
