import { assembleRecording, CaptureIntegrityError } from './recordingChunks.ts'
export { CaptureIntegrityError } from './recordingChunks.ts'
const DATABASE_NAME = 'multicam-recordings'
const DATABASE_VERSION = 1

export type LocalCaptureState = 'recording' | 'stored' | 'uploading' | 'uploaded' | 'validating' | 'verified' | 'failed'
export interface LocalCapture {
  capture_id: string
  take_id?: string
  session_id: string
  device_id: string
  role: string
  mime_type: string
  state: LocalCaptureState
  created_at: string
  updated_at: string
  chunk_count: number
  size_bytes: number
  first_chunk_size?: number
  last_chunk_size?: number
  total_size?: number
  error?: string
  verification_version?: number
  stream_settings: Record<string, unknown>
}
interface StoredChunk { capture_id: string; index: number; data: Blob }
interface StoredTelemetry { capture_id: string; index: number; event: unknown }
let databasePromise: Promise<IDBDatabase> | null = null
function database(): Promise<IDBDatabase> {
  if (databasePromise) return databasePromise
  databasePromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION)
    request.onerror = () => { databasePromise = null; reject(request.error) }
    request.onupgradeneeded = () => {
      const db = request.result
      const captures = db.createObjectStore('captures', { keyPath: 'capture_id' })
      captures.createIndex('device_id', 'device_id')
      for (const name of ['chunks', 'telemetry']) {
        const store = db.createObjectStore(name, { keyPath: ['capture_id', 'index'] })
        store.createIndex('capture_id', 'capture_id')
      }
    }
    request.onsuccess = () => {
      const db = request.result
      db.onversionchange = () => { db.close(); databasePromise = null }
      db.onclose = () => { databasePromise = null }
      resolve(db)
    }
  })
  return databasePromise
}
function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve()
    transaction.onerror = () => reject(transaction.error ?? new Error('IndexedDB write failed'))
    transaction.onabort = () => reject(transaction.error ?? new Error('IndexedDB transaction aborted'))
  })
}
function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
}
export async function createLocalCapture(capture: LocalCapture): Promise<void> {
  const db = await database()
  const tx = db.transaction('captures', 'readwrite')
  const done = transactionDone(tx)
  tx.objectStore('captures').add(capture)
  await done
}
export async function appendRecordingChunk(captureId: string, index: number, data: Blob): Promise<void> {
  if (!Number.isInteger(index) || index < 0 || !data.size) throw new CaptureIntegrityError(`Neplatný blok ${index}.`)
  const db = await database()
  const tx = db.transaction(['captures', 'chunks'], 'readwrite')
  const done = transactionDone(tx)
  const captures = tx.objectStore('captures')
  const request = captures.get(captureId)
  // Queue dependent writes in the IDB success event itself: never yield a live
  // readwrite transaction across an await (notably on Safari).
  request.onsuccess = () => {
    const capture = request.result as LocalCapture | undefined
    if (!capture) { tx.abort(); return }
    tx.objectStore('chunks').add({ capture_id: captureId, index, data } satisfies StoredChunk)
    capture.chunk_count += 1
    capture.size_bytes += data.size
    capture.total_size = capture.size_bytes
    if (index === 0) capture.first_chunk_size = data.size
    capture.last_chunk_size = data.size
    capture.updated_at = new Date().toISOString()
    captures.put(capture)
  }
  await done
}
export async function appendTelemetryEvent(captureId: string, index: number, event: unknown): Promise<void> {
  const db = await database()
  const tx = db.transaction('telemetry', 'readwrite')
  const done = transactionDone(tx)
  tx.objectStore('telemetry').add({ capture_id: captureId, index, event } satisfies StoredTelemetry)
  await done
}
export async function setLocalCaptureState(captureId: string, state: LocalCaptureState, error?: string): Promise<void> {
  const db = await database()
  const tx = db.transaction('captures', 'readwrite')
  const done = transactionDone(tx)
  const store = tx.objectStore('captures')
  const request = store.get(captureId)
  request.onsuccess = () => {
    const capture = request.result as LocalCapture | undefined
    if (!capture) { tx.abort(); return }
    capture.state = state
    if (state === 'verified') capture.verification_version = 2
    capture.error = error
    capture.updated_at = new Date().toISOString()
    store.put(capture)
  }
  await done
}
export async function listLocalCaptures(deviceId?: string): Promise<LocalCapture[]> {
  const db = await database()
  const tx = db.transaction('captures', 'readonly')
  const done = transactionDone(tx)
  const store = tx.objectStore('captures')
  const [captures] = await Promise.all([
    requestResult(deviceId ? store.index('device_id').getAll(deviceId) : store.getAll()) as Promise<LocalCapture[]>, done,
  ])
  return captures.sort((left, right) => right.created_at.localeCompare(left.created_at))
}
export async function readLocalArtifacts(captureId: string, mimeType: string): Promise<{ recording: Blob; telemetry: Blob }> {
  const db = await database()
  const tx = db.transaction(['captures', 'chunks', 'telemetry'], 'readonly')
  const done = transactionDone(tx)
  // Schedule all reads before awaiting; the transaction must not auto-commit
  // between getAll(chunks) and getAll(telemetry).
  const [capture, chunks, telemetry] = await Promise.all([
    requestResult(tx.objectStore('captures').get(captureId)) as Promise<LocalCapture | undefined>,
    requestResult(tx.objectStore('chunks').index('capture_id').getAll(captureId)) as Promise<StoredChunk[]>,
    requestResult(tx.objectStore('telemetry').index('capture_id').getAll(captureId)) as Promise<StoredTelemetry[]>, done,
  ])
  if (!capture) throw new CaptureIntegrityError('Záznam chybí v lokální databázi.')
  if (mimeType !== capture.mime_type) throw new CaptureIntegrityError('Typ média neodpovídá uloženému záznamu.')
  try {
    const recording = await assembleRecording(chunks, capture.chunk_count, capture.size_bytes, capture.mime_type)
    const sorted = [...chunks].sort((a, b) => a.index - b.index)
    console.info('capture.assembled', { capture_id: captureId, chunks: chunks.length,
      indexes: `0..${chunks.length - 1}`, first_chunk_size: sorted[0]?.data.size,
      last_chunk_size: sorted.at(-1)?.data.size, total_size: recording.size, mime_type: capture.mime_type })
    telemetry.sort((a, b) => a.index - b.index)
    return { recording, telemetry: new Blob(telemetry.map(sample => `${JSON.stringify(sample.event)}\n`), { type: 'application/x-ndjson' }) }
  } catch (error) {
    console.error('capture.integrity_failed', { capture_id: captureId, indexes: chunks.map(chunk => chunk.index), error: String(error) })
    throw error
  }
}
export async function finalizeLocalCapture(captureId: string, expectedCount: number): Promise<void> {
  const capture = (await listLocalCaptures()).find(item => item.capture_id === captureId)
  if (!capture || capture.chunk_count !== expectedCount) throw new CaptureIntegrityError('Ne všechny bloky MediaRecorderu byly uloženy.')
  await readLocalArtifacts(captureId, capture.mime_type)
  await setLocalCaptureState(captureId, 'stored')
}
export async function deleteLocalCapture(captureId: string): Promise<void> {
  const db = await database()
  const tx = db.transaction(['captures', 'chunks', 'telemetry'], 'readwrite')
  const done = transactionDone(tx)
  const request = tx.objectStore('captures').get(captureId)
  request.onsuccess = () => {
    if (request.result?.state !== 'verified' || request.result.verification_version !== 2) { tx.abort(); return }
    tx.objectStore('captures').delete(captureId)
    for (const name of ['chunks', 'telemetry']) {
      const index = tx.objectStore(name).index('capture_id')
      const keys = index.getAllKeys(captureId)
      keys.onsuccess = () => { for (const key of keys.result) index.objectStore.delete(key) }
    }
  }
  await done
}

export async function recoverLocalCaptures(): Promise<void> {
  for (const capture of await listLocalCaptures()) {
    if (capture.state === 'verified' && capture.verification_version !== 2) {
      await setLocalCaptureState(capture.capture_id, 'stored', 'Starší potvrzení ověřilo pouze přenos. Před smazáním je nutné nové ověření média serverem.')
    } else if (capture.state === 'recording') {
      await setLocalCaptureState(capture.capture_id, 'failed', 'Aplikace skončila před dokončením záznamu. Bloky zůstaly uložené; před ručním odesláním se ověří jejich úplnost.')
    } else if (['uploading', 'uploaded', 'validating'].includes(capture.state)) {
      await setLocalCaptureState(capture.capture_id, 'stored')
    }
  }
}
