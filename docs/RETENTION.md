# Audio retention and deletion

The application stores private content on one local persistent volume. It has no third-party audio upload integration; the deployment worker has no network interface. Encryption at rest and backup encryption are host/operator responsibilities.

| Data | Retention and deletion |
| --- | --- |
| Original recording, selected normalized clip, separated stems and child copies | Retained with the project until explicitly deleted |
| Raw detections/model arrays, saved note revisions, canonical scores and generated exports | Retained with the project; deletion removes the whole project directory and related DB rows |
| Failed/canceled unpublished mixed attempts | Removed after their process/file leases end; original input remains |
| API preview/engraving/waveform scratch directories | Removed on normal request completion or handled failure; abrupt API termination can leave an orphan until stopped-service maintenance |
| Accounts and password hashes | Retained until operator policy removes the account; disabling revokes sessions but is not content deletion |
| Sessions | Expire after eight hours by default; logout/reset/disable revokes them; at most 20 per account; expired rows are pruned when a new session is created |
| Rate-limit buckets | Hashed keys, pruned after 24 hours during rate-limit activity |
| Processing quota ledger | Owner ID and attempt timestamp for the rolling 24-hour limit, pruned when new attempts are created; deleting content cannot evade quota |
| Upload replay markers | Owner/key/timestamp, pruned after seven days on the next upload; fingerprint is cleared when its project is deleted, preventing retry resurrection without retaining audio metadata |
| Unfinished resumable uploads and playback previews | Private `uploads/<UUID>` data expires after 24 idle hours by default; each acknowledged chunk renews expiry; explicit discard and the worker janitor reclaim bytes. Canceled/expired markers retain no audio metadata and are pruned after seven days |
| Real model-window checkpoints | Private `.analysis-checkpoints` survive failed/interrupted attempts for retry; completion/cancellation deletes them after leases release; failed checkpoints expire after 24 hours; project deletion removes them |
| Upload request spools and decoder scratch | Private disk-backed `.request-spool` and `.scratch`; normal response cleanup removes temporary files; abrupt termination can require stopped-service cleanup |
| Coordination lockfiles | Empty UUID-named files remain to avoid unsafe lock-inode reuse; contain no recording, title or score |
| Backups and existing client downloads | Outside application deletion; operator/client retention applies |

Deleting a project immediately tombstones it, cancels/fences its jobs and removes it from lists and authorized reads. Deleting a mixed parent also deletes every selected-part child. Deleting any source also deletes its derived piano accompaniments, recursively, so copied recordings cannot survive source deletion. Deleting a derived accompaniment leaves its original and sibling arrangements intact. The confirmation dialog describes this cascade. Derived accompaniments own private copies of the source recording, normalized clip, raw events/MIDI when available, and their own notes/scores; these copies count against the same account storage quota. Existing response streams may finish after deletion is requested, since already authorized bytes cannot be recalled. Cross-process file leases prevent physical cleanup from racing a live stream, save, decoder or model attempt. The continuously running worker retries cleanup after restart and removes recordings, stems, edits, exports, project/job records, note identities and score records. If a process or storage permission blocks cleanup, the tombstone remains inaccessible and physical deletion waits for operator repair/lease release. Monitor pending tombstones; do not describe HTTP 202 as proof that disk removal already finished.

SQLite `secure_delete` overwrites freed database cells when purging content. This is not a promise of forensic erasure from WAL files, SSDs, filesystem snapshots, external backups or browser downloads. Preserve only encrypted backups for the published retention period and avoid restoring previously deleted content to a public service without reconciling deletion history.

An abrupt API kill can leave temporary directories beginning `audio-preview-`, `review-waveform-`, `score-midi-`, or `score-pdf-` in `DATA_DIR`. They are private and never statically served. During scheduled maintenance, **stop both API and worker**, inspect and remove only these known scratch directories, then restart. Do not sweep them while requests may still own them. Project deletion cleanup itself is automatic and lease-protected; scratch cleanup is an explicit operator task in this version. Publish that distinction to users before launch. A target-host retention policy and backup-expiry schedule are launch requirements, not assumed background services.
