import 'fake-indexeddb/auto'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createLocalCapture, appendRecordingChunk, appendTelemetryEvent, finalizeLocalCapture, readLocalArtifacts } from '../src/recordingStore.ts'
import { uploadArtifact } from '../src/api.ts'
const [url, path, session_id, device_id, capture_id, take_id] = process.argv.slice(2)
const bytes = await readFile(path)
const capture = { session_id, device_id, capture_id, take_id, role: 'secondary_camera', state: 'recording',
  mime_type: 'video/webm', created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  chunk_count: 0, size_bytes: 0, stream_settings: {} }
await createLocalCapture(capture)
let count = 0
for (let offset = 0; offset < bytes.length; offset += 65536) {
  await appendRecordingChunk(capture_id, count++, new Blob([bytes.subarray(offset, offset + 65536)]))
}
await appendTelemetryEvent(capture_id, 0, { event: 'recording_started', recording_offset_ms: 0 })
await finalizeLocalCapture(capture_id, count)
const artifacts = await readLocalArtifacts(capture_id, capture.mime_type)
assert.deepEqual(Buffer.from(await artifacts.recording.arrayBuffer()), bytes)
const nativeFetch = globalThis.fetch
globalThis.fetch = (path, options) => nativeFetch(new URL(path, url), options)
const video = await uploadArtifact(session_id, device_id, capture_id, 'recording', artifacts.recording, () => {}, take_id)
assert.equal(video.verified, true)
assert.equal(video.media_verified, true)
const telemetry = await uploadArtifact(session_id, device_id, capture_id, 'telemetry', artifacts.telemetry, () => {}, take_id)
assert.equal(telemetry.verified, true)
console.log(JSON.stringify({ verified: true, chunks: count }))
