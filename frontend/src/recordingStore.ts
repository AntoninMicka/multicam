const DATABASE_NAME = 'multicam-recordings'
const DATABASE_VERSION = 1

export type LocalCaptureState = 'recording' | 'stored' | 'uploading' | 'verified'

export interface LocalCapture {
  capture_id: string
  session_id: string
  device_id: string
  role: string
  mime_type: string
  state: LocalCaptureState
  created_at: string
  updated_at: string
  chunk_count: number
  size_bytes: number
  stream_settings: Record<string, unknown>
}

interface StoredChunk {
  capture_id: string
  index: number
  data: Blob
}

interface StoredTelemetry {
  capture_id: string
  index: number
  event: unknown
}

let databasePromise: Promise<IDBDatabase> | null = null

function database(): Promise<IDBDatabase> {
  if (databasePromise) return databasePromise
  databasePromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION)
    request.onerror = () => reject(request.error)
    request.onupgradeneeded = () => {
      const db = request.result
      const captures = db.createObjectStore('captures', { keyPath: 'capture_id' })
      captures.createIndex('device_id', 'device_id')
      const chunks = db.createObjectStore('chunks', { keyPath: ['capture_id', 'index'] })
      chunks.createIndex('capture_id', 'capture_id')
      const telemetry = db.createObjectStore('telemetry', { keyPath: ['capture_id', 'index'] })
      telemetry.createIndex('capture_id', 'capture_id')
    }
    request.onsuccess = () => resolve(request.result)
  })
  return databasePromise
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve()
    transaction.onerror = () => reject(transaction.error)
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
  const transaction = db.transaction('captures', 'readwrite')
  transaction.objectStore('captures').put(capture)
  await transactionDone(transaction)
}

export async function appendRecordingChunk(captureId: string, index: number, data: Blob): Promise<void> {
  const db = await database()
  const transaction = db.transaction(['captures', 'chunks'], 'readwrite')
  const captures = transaction.objectStore('captures')
  const capture = await requestResult(captures.get(captureId)) as LocalCapture | undefined
  if (!capture) throw new Error('Lokální záznam nebyl nalezen.')
  transaction.objectStore('chunks').put({ capture_id: captureId, index, data } satisfies StoredChunk)
  capture.chunk_count = Math.max(capture.chunk_count, index + 1)
  capture.size_bytes += data.size
  capture.updated_at = new Date().toISOString()
  captures.put(capture)
  await transactionDone(transaction)
}

export async function appendTelemetryEvent(captureId: string, index: number, event: unknown): Promise<void> {
  const db = await database()
  const transaction = db.transaction('telemetry', 'readwrite')
  transaction.objectStore('telemetry').put({ capture_id: captureId, index, event } satisfies StoredTelemetry)
  await transactionDone(transaction)
}

export async function setLocalCaptureState(captureId: string, state: LocalCaptureState): Promise<void> {
  const db = await database()
  const transaction = db.transaction('captures', 'readwrite')
  const store = transaction.objectStore('captures')
  const capture = await requestResult(store.get(captureId)) as LocalCapture | undefined
  if (!capture) throw new Error('Lokální záznam nebyl nalezen.')
  capture.state = state
  capture.updated_at = new Date().toISOString()
  store.put(capture)
  await transactionDone(transaction)
}

export async function listLocalCaptures(deviceId: string): Promise<LocalCapture[]> {
  const db = await database()
  const transaction = db.transaction('captures', 'readonly')
  const captures = await requestResult(transaction.objectStore('captures').index('device_id').getAll(deviceId)) as LocalCapture[]
  await transactionDone(transaction)
  return captures.sort((left, right) => right.created_at.localeCompare(left.created_at))
}

export async function readLocalArtifacts(captureId: string, mimeType: string): Promise<{ recording: Blob; telemetry: Blob }> {
  const db = await database()
  const transaction = db.transaction(['chunks', 'telemetry'], 'readonly')
  const chunks = await requestResult(
    transaction.objectStore('chunks').index('capture_id').getAll(captureId),
  ) as StoredChunk[]
  const telemetry = await requestResult(
    transaction.objectStore('telemetry').index('capture_id').getAll(captureId),
  ) as StoredTelemetry[]
  await transactionDone(transaction)
  chunks.sort((left, right) => left.index - right.index)
  telemetry.sort((left, right) => left.index - right.index)
  return {
    recording: new Blob(chunks.map((chunk) => chunk.data), { type: mimeType }),
    telemetry: new Blob(telemetry.map((sample) => `${JSON.stringify(sample.event)}\n`), { type: 'application/x-ndjson' }),
  }
}

async function deleteByCapture(index: IDBIndex, captureId: string): Promise<void> {
  const keys = await requestResult(index.getAllKeys(captureId))
  for (const key of keys) index.objectStore.delete(key)
}

export async function deleteLocalCapture(captureId: string): Promise<void> {
  const db = await database()
  const transaction = db.transaction(['captures', 'chunks', 'telemetry'], 'readwrite')
  transaction.objectStore('captures').delete(captureId)
  await Promise.all([
    deleteByCapture(transaction.objectStore('chunks').index('capture_id'), captureId),
    deleteByCapture(transaction.objectStore('telemetry').index('capture_id'), captureId),
  ])
  await transactionDone(transaction)
}
