export type RecoveryAction = 'wait' | 'stop' | 'replace_stream' | 'start_segment' | 'ready'
export function recoveryAction(serverState: string, recorderState: string | undefined, tracksLive: boolean, finalizing: boolean): RecoveryAction {
  if (finalizing) return 'wait'
  if (recorderState && recorderState !== 'inactive') {
    if (serverState !== 'recording' || !tracksLive) return 'stop'
    return 'ready'
  }
  if (!tracksLive) return 'replace_stream'
  return serverState === 'recording' ? 'start_segment' : 'ready'
}
export function needsReconnect(readyState: number | undefined, lastHeardAt: number, now: number): boolean {
  return readyState !== 1 || now - lastHeardAt > 20_000
}
