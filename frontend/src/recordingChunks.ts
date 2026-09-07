export interface RecordingChunk { index: number; data: Blob }
export class CaptureIntegrityError extends Error {
  readonly code = 'capture_integrity_failed'
}

export async function assembleRecording(chunks: RecordingChunk[], expectedCount: number, expectedSize: number, mimeType: string): Promise<Blob> {
  const ordered = [...chunks].sort((a, b) => a.index - b.index)
  if (!ordered.length || ordered[0]!.index !== 0) throw new CaptureIntegrityError('Chybí první blok (chunk 0) s inicializací videa. Záznam nelze bezpečně odeslat.')
  if (ordered.length !== expectedCount) throw new CaptureIntegrityError(`Počet bloků nesouhlasí: ${ordered.length}, očekáváno ${expectedCount}.`)
  for (let index = 0; index < ordered.length; index++) {
    const chunk = ordered[index]!
    if (chunk.index !== index) throw new CaptureIntegrityError(`Chybějící nebo duplicitní blok: očekáván ${index}, nalezen ${chunk.index}.`)
    if (!chunk.data.size) throw new CaptureIntegrityError(`Blok ${index} je prázdný.`)
  }
  const blob = new Blob(ordered.map(chunk => chunk.data), { type: mimeType })
  if (blob.size !== expectedSize) throw new CaptureIntegrityError(`Velikost záznamu nesouhlasí: ${blob.size}, očekáváno ${expectedSize}.`)
  if (mimeType.toLowerCase().startsWith('video/webm')) {
    const header = new Uint8Array(await blob.slice(0, 4).arrayBuffer())
    if (![0x1a, 0x45, 0xdf, 0xa3].every((byte, index) => header[index] === byte)) {
      throw new CaptureIntegrityError('Video nemá WebM/EBML hlavičku. Lokální kopie zůstává zachována; záznam vyžaduje kontrolu.')
    }
  }
  return blob
}

/** One queue per recorder. Errors remain latched even when subsequent writes succeed. */
export class RecordingWriteQueue {
  private pending: Promise<void> = Promise.resolve()
  private failure: unknown = null
  enqueue(write: () => Promise<void>): void {
    this.pending = this.pending.then(write).catch(error => { this.failure ??= error })
  }
  async drain(): Promise<void> {
    await this.pending
    if (this.failure) throw this.failure
  }
}
