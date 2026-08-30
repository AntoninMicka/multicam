async function json(response) {
    if (!response.ok)
        throw new Error((await response.json()).detail ?? 'Požadavek selhal');
    return response.json();
}
export function createSession(name) {
    return fetch('/api/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
    }).then((json));
}
export function getSession(id) {
    return fetch(`/api/sessions/${id}`).then((json));
}
export function getCurrentSession() {
    return fetch('/api/sessions/current').then((json));
}
export function registerDevice(sessionId, name, role, capabilities, deviceId) {
    return fetch(`/api/sessions/${sessionId}/devices`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, role, device_id: deviceId, capabilities }),
    }).then((json));
}
export function sessionSocket(sessionId, deviceId) {
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const query = deviceId ? `?device_id=${deviceId}` : '';
    return new WebSocket(`${protocol}://${location.host}/api/ws/${sessionId}${query}`);
}
