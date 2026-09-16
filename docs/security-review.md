# Marketplace security feedback review — 2026-09-15

Reviewed the current working tree based on commit
`d08818b4dc6fe58c7995b9e555ab377722c23e34` against all current comments on
[the author's marketplace issues](https://github.com/omacom/omarchy-plugin-marketplace/issues?q=is%3Aissue%20author%3Atuthan).
The search returned nine issues and 38 comments, including five substantive
reviewer security comments. Open and closed issues were included. Historical
edits and deleted comments are outside this snapshot.

## Findings and disposition

| Prior feedback | SteamOS Companion evidence and disposition |
|---|---|
| [Predictable shared temporary files, missing owner/permission/link checks (#146)](https://github.com/omacom/omarchy-plugin-marketplace/issues/146#issuecomment-5292299510) | No shared `/tmp` fallback. Private state lives under `${XDG_STATE_HOME:-~/.local/state}/steamos-companion`. Hardened both credential and pending records with no-follow directory traversal, owner checks, descriptor-relative reads/replacements/deletes, exclusive random temporary files, 0700 directories and 0600 files. Hardlinks and special files are refused. Reads and writes are capped at 64 KiB. |
| [Ancestor swap across pathname-based replacement (#5141)](https://github.com/omacom/omarchy-plugin-marketplace/issues/5141#issuecomment-5671333542) | Applicable to the previous credential writer despite its atomic rename. Fixed by retaining the opened directory descriptor throughout each operation. All ancestor symlinks are rejected; ancestors must be owned by root/current user and not group/world-writable, except trusted sticky directories. No pathname-based chmod remains. |
| [HTTP credentials and unverified HTTPS (#2615)](https://github.com/omacom/omarchy-plugin-marketplace/issues/2615#issuecomment-5429804983) | Already protected: HTTPS-only endpoint validation and SHA-256 certificate pin verification before any request or credential is written. `CERT_NONE` disables public-CA verification because the host uses a pinned certificate; it does not bypass the explicit pin. Discovery sends no credentials. Initial discovery identity still requires the owner to compare pairing codes on both devices. |
| [Unbounded remote bodies/lists/text and rich-text resource loads (#2615)](https://github.com/omacom/omarchy-plugin-marketplace/issues/2615#issuecomment-5429804983) | Existing 256 KiB response body cap retained. Added limits of 16 outputs, 256 elements per other list (including modes), 1024 characters per string, 128 keys per object, 16 nesting levels, and 4096 visited values/keys. Oversized responses are rejected, so action IDs are never silently truncated. Discovery exposes at most 32 results and reports truncation. |
| [QML AutoText, external CLI startup and remote shell installation (#1246)](https://github.com/omacom/omarchy-plugin-marketplace/issues/1246#issuecomment-5371905567) | Local BodyText, HintText and verification-code Text explicitly use PlainText. Inspected installed `qs.Ui.Button` label and tooltip sinks: also PlainText. The bar tooltip contains local fixed status words/timestamps. Python helper code ships in this repository and runs via an argv array; no downloaded CLI, runtime installer, streamed shell command, or arbitrary command endpoint exists. Python itself is a system dependency. |
| [Unbounded stdout/stderr collectors (#1246)](https://github.com/omacom/omarchy-plugin-marketplace/issues/1246#issuecomment-5379030007) | Applicable: both helper streams previously used StdioCollector. Replaced with immediate SplitParser chunks, independent conservative UTF-8 byte budgets (2 MiB stdout / 64 KiB stderr), overflow latch, SIGKILL, buffer discard, and refusal to parse overflowed output. Existing timeout escalation remains; timed-out output cannot report success. Mutations interrupted or rejected after dispatch report an unknown outcome. |

## Validation

- All 30 tests pass: 23 existing tests and seven new security regressions in `tests/test_security.py`.
- Tests actively swap a parent directory before rename and a leaf after open, and verify that unrelated targets are not written, read, or chmodded.
- Tests reject ancestor symlinks, writable ancestors, hardlinks, FIFOs, oversized state, oversized response bodies/lists/text, excessive nesting and non-finite numbers.
- A Node harness executes the actual QML collection and settlement functions against newline-free chunks, exact limits, multibyte text, stderr overflow, discarded success output, and timeouts.
- `qmllint Panel.qml BarWidget.qml` and `git diff --check` pass.
- Filesystem tests run outside the sandbox because its root-owned directories appear as UID 65534. The owner checks are intentionally not weakened for that environment.

These checks cover the reported patterns, not every possible vulnerability.
The QML harness is not an installed-shell integration test. The separate Decky
host, physical power/display operations, and future changes to Omarchy's shared
UI components need their own validation. No installed plugin, host, or remote
repository was changed by this review.

## Checklist for future releases

- [ ] Fetch every page of issues and comments, including closed tickets; record source links and exact reviewed revision.
- [ ] Keep untrusted text PlainText, including shared button, tooltip and dialog components.
- [ ] Cap bytes before collecting/parsing, then list counts, text lengths, nesting and total objects before rendering. A timeout is not a byte cap.
- [ ] Terminate overflowing producers, latch failure, discard buffered success, and preserve unknown mutation outcomes.
- [ ] Validate HTTPS identity before sending authentication or pairing material. Keep bootstrap discovery unauthenticated and require out-of-band code comparison.
- [ ] Bind filesystem operations to opened directory/file descriptors; reject symlinks, unsafe owners, hardlinks and special files. Atomic rename alone does not validate ancestors.
- [ ] Keep runtime helpers bundled and reviewed; use argv and stdin, not interpolated shell commands or remote shell installers.
- [ ] Verify `schemaVersion` spelling and the exact submitted SHA; a passing marketplace baseline is limited pattern coverage, not a security audit.
- [ ] Run the security suite and QML lint; verify live rendering/overflow behavior when the shell or parser changes.
