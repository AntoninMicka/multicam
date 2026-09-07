import 'fake-indexeddb/auto'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { assembleRecording, RecordingWriteQueue } from '../src/recordingChunks.ts'
import { createLocalCapture, appendRecordingChunk, appendTelemetryEvent, readLocalArtifacts, finalizeLocalCapture, listLocalCaptures, deleteLocalCapture, setLocalCaptureState } from '../src/recordingStore.ts'

const chunks = [new Blob(['HEADER']), new Blob(['DATA1']), new Blob(['DATA2'])].map((data, index) => ({ data, index }))
for (const order of [[0, 1, 2], [2, 0, 1]]) {
  test(`assembly explicitly orders ${order}`, async () => {
    assert.equal(await (await assembleRecording(order.map(index => chunks[index]), 3, 16, 'video/mp4')).text(), 'HEADERDATA1DATA2')
  })
}
for (const indexes of [[0, 1, 3], [1, 2, 3], [0, 1, 1]]) {
  test(`rejects missing/duplicate indexes ${indexes}`, async () => {
    await assert.rejects(assembleRecording(indexes.map(index => ({ index, data: new Blob(['x']) })), 3, 3, 'video/mp4'), /blok|block/i)
  })
}
test('rejects empty chunk and inconsistent metadata', async () => {
  await assert.rejects(assembleRecording([{ index: 0, data: new Blob([]) }], 1, 0, 'video/mp4'), /prázdný/)
  await assert.rejects(assembleRecording(chunks, 4, 16, 'video/mp4'), /Počet/)
  await assert.rejects(assembleRecording(chunks, 3, 18, 'video/mp4'), /Velikost/)
  await assert.rejects(assembleRecording(chunks, 3, 16, 'video/webm'), /EBML/)
})
function capture() {
  return { capture_id: crypto.randomUUID(), session_id: crypto.randomUUID(), device_id: crypto.randomUUID(), role: 'secondary_camera',
    mime_type: 'video/mp4', state: 'recording', created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    chunk_count: 0, size_bytes: 0, stream_settings: {} }
}
test('IndexedDB commits chunk zero and exact ordered byte concatenation', async () => {
  const item = capture()
  await createLocalCapture(item)
  await Promise.all(chunks.map(chunk => appendRecordingChunk(item.capture_id, chunk.index, chunk.data)))
  await appendTelemetryEvent(item.capture_id, 0, { event: 'recording_started' })
  await finalizeLocalCapture(item.capture_id, 3)
  const artifacts = await readLocalArtifacts(item.capture_id, item.mime_type)
  assert.equal(await artifacts.recording.text(), 'HEADERDATA1DATA2')
  const stored = (await listLocalCaptures()).find(c => c.capture_id === item.capture_id)
  assert.equal(stored.first_chunk_size, 6)
  assert.equal(stored.last_chunk_size, 5)
  assert.equal(stored.total_size, 16)
  await assert.rejects(deleteLocalCapture(item.capture_id))
})
test('regression: failed first write plus later chunks cannot be finalized or uploaded', async () => {
  const item = capture()
  await createLocalCapture(item)
  const queue = new RecordingWriteQueue()
  queue.enqueue(async () => { throw new DOMException('First IDB write aborted', 'AbortError') })
  queue.enqueue(() => appendRecordingChunk(item.capture_id, 1, chunks[1].data))
  queue.enqueue(() => appendRecordingChunk(item.capture_id, 2, chunks[2].data))
  await assert.rejects(queue.drain(), /First IDB/)
  await assert.rejects(readLocalArtifacts(item.capture_id, item.mime_type), /chunk 0/)
  await assert.rejects(finalizeLocalCapture(item.capture_id, 3))
  assert.equal((await listLocalCaptures()).find(c => c.capture_id === item.capture_id).state, 'recording')
})
test('duplicate writes abort atomically without overwriting chunk zero or inflating metadata', async () => {
  const item = capture()
  await createLocalCapture(item)
  await appendRecordingChunk(item.capture_id, 0, new Blob(['HEADER']))
  await assert.rejects(appendRecordingChunk(item.capture_id, 0, new Blob(['BAD'])))
  const artifact = await readLocalArtifacts(item.capture_id, item.mime_type)
  assert.equal(await artifact.recording.text(), 'HEADER')
})
test('final dataavailable write and telemetry finish before stored; later recorder has isolated queue', async () => {
  const item = capture()
  await createLocalCapture(item)
  const queue = new RecordingWriteQueue()
  queue.enqueue(() => appendRecordingChunk(item.capture_id, 0, chunks[0].data))
  // The final dataavailable runs before stop, but its asynchronous IDB write is still pending.
  queue.enqueue(async () => { await new Promise(resolve => setTimeout(resolve, 20)); await appendRecordingChunk(item.capture_id, 1, chunks[1].data) })
  queue.enqueue(() => appendTelemetryEvent(item.capture_id, 0, { event: 'recording_stopped' }))
  const nextRecorder = new RecordingWriteQueue()
  await nextRecorder.drain()
  await queue.drain()
  await finalizeLocalCapture(item.capture_id, 2)
  assert.equal(await (await readLocalArtifacts(item.capture_id, item.mime_type)).recording.text(), 'HEADERDATA1')
  await setLocalCaptureState(item.capture_id, 'verified')
  await deleteLocalCapture(item.capture_id)
})

test('old transport-only verified flag does not permit deletion', async () => {
  const item = { ...capture(), state: 'verified' }
  await createLocalCapture(item)
  await assert.rejects(deleteLocalCapture(item.capture_id))
})
