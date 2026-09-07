import assert from 'node:assert/strict'
import { test } from 'node:test'
import { recoveryAction, needsReconnect } from '../src/phoneRecovery.ts'

test('sleep recovery detects silently dead socket and stale heartbeat', () => {
  assert.equal(needsReconnect(1, 0, 21000), true)
  assert.equal(needsReconnect(3, 20000, 21000), true)
  assert.equal(needsReconnect(1, 20000, 21000), false)
})
test('missed stop or closed session finalizes the old recorder', () => {
  assert.equal(recoveryAction('stopped', 'recording', true, false), 'stop')
  assert.equal(recoveryAction('closed', 'recording', true, false), 'stop')
})
test('dead tracks finish old segment before a new camera stream is acquired', () => {
  assert.equal(recoveryAction('recording', 'recording', false, false), 'stop')
  assert.equal(recoveryAction('recording', 'inactive', false, true), 'wait')
  assert.equal(recoveryAction('recording', 'inactive', false, false), 'replace_stream')
  assert.equal(recoveryAction('recording', 'inactive', true, false), 'start_segment')
  assert.equal(recoveryAction('recording', 'recording', true, false), 'ready')
})
