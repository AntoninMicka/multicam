import { computed, nextTick, onBeforeUnmount, ref } from 'vue';
import { createSession, getCurrentSession, getSession, registerDevice, sessionSocket } from './api';
const role = ref(null);
const session = ref(null);
const sessionName = ref('Zkušební relace');
const deviceName = ref(`Telefon ${Math.floor(Math.random() * 90 + 10)}`);
const deviceId = ref(localStorage.getItem('multicam.deviceId') ?? '');
const error = ref('');
const busy = ref(false);
const flashVisible = ref(false);
const preview = ref(null);
const recording = ref(false);
const recordingUrl = ref('');
const cameraReady = ref(false);
let socket = null;
let flashTimer;
let mediaStream = null;
let mediaRecorder = null;
let recordedChunks = [];
const devices = computed(() => Object.values(session.value?.devices ?? {}));
async function readCapabilities() {
    const estimate = await navigator.storage?.estimate?.().catch(() => undefined);
    const permission = async (name) => {
        try {
            const result = await navigator.permissions.query({ name });
            return result.state === 'granted' ? true : result.state === 'denied' ? false : null;
        }
        catch {
            return null;
        }
    };
    let batteryPercent = null;
    try {
        const battery = await navigator.getBattery?.();
        if (battery)
            batteryPercent = Math.round(battery.level * 100);
    }
    catch { /* API není dostupné na všech telefonech. */ }
    return {
        battery_percent: batteryPercent,
        free_storage_bytes: estimate?.quota && estimate.usage !== undefined ? estimate.quota - estimate.usage : null,
        camera_permission: await permission('camera'),
        microphone_permission: await permission('microphone'),
    };
}
function formatBytes(value) {
    if (value === null)
        return 'neznámé';
    return `${(value / 1024 ** 3).toFixed(1)} GB`;
}
function permissionLabel(value) {
    return value === true ? 'ano' : value === false ? 'ne' : 'nezjištěno';
}
function connectSocket(id, cameraId) {
    socket?.close();
    socket = sessionSocket(id, cameraId);
    socket.onmessage = (event) => {
        const message = JSON.parse(event.data);
        if (message.type === 'session.snapshot' || message.type === 'session.updated')
            session.value = message.payload;
        if (message.type === 'clap.trigger' && role.value === 'main_camera')
            flashClap();
        if (message.type === 'recording.start' && role.value !== 'director')
            startLocalRecording();
        if (message.type === 'recording.stop' && role.value !== 'director')
            stopLocalRecording();
    };
    socket.onerror = () => (error.value = 'Spojení se serverem bylo přerušeno.');
}
async function startDirector() {
    busy.value = true;
    error.value = '';
    try {
        session.value = await createSession(sessionName.value);
        connectSocket(session.value.session_id);
    }
    catch (reason) {
        error.value = reason instanceof Error ? reason.message : 'Relaci nelze vytvořit.';
    }
    finally {
        busy.value = false;
    }
}
async function joinCamera() {
    busy.value = true;
    error.value = '';
    try {
        const activeSession = await getCurrentSession();
        const capabilities = await readCapabilities();
        const cameraRole = role.value === 'main_camera'
            ? 'main_camera'
            : role.value === 'top_camera' ? 'top_camera' : 'secondary_camera';
        const device = await registerDevice(activeSession.session_id, deviceName.value, cameraRole, capabilities, deviceId.value || undefined);
        deviceId.value = device.device_id;
        localStorage.setItem('multicam.deviceId', device.device_id);
        session.value = await getSession(activeSession.session_id);
        connectSocket(activeSession.session_id, device.device_id);
        await nextTick();
        await prepareCamera();
        if (activeSession.state === 'recording')
            startLocalRecording();
    }
    catch (reason) {
        error.value = reason instanceof Error ? reason.message : 'K relaci se nelze připojit.';
    }
    finally {
        busy.value = false;
    }
}
function triggerClap() {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        error.value = 'Hlavní kamera není připojená k řídicímu kanálu.';
        return;
    }
    socket.send(JSON.stringify({ type: 'clap.trigger', payload: { requested_at: new Date().toISOString() } }));
}
function sendRecordingCommand(type) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        error.value = 'Řídicí kanál není připojený.';
        return;
    }
    socket.send(JSON.stringify({ type, payload: { requested_at: new Date().toISOString() } }));
}
async function prepareCamera() {
    try {
        mediaStream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode: { ideal: 'environment' } },
            audio: true,
        });
        if (preview.value)
            preview.value.srcObject = mediaStream;
        cameraReady.value = true;
    }
    catch {
        error.value = 'Kamera nebo mikrofon nejsou dostupné. Zkontrolujte oprávnění a HTTPS.';
    }
}
function startLocalRecording() {
    if (!mediaStream || recording.value)
        return;
    try {
        const preferredTypes = ['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm', 'video/mp4'];
        const mimeType = preferredTypes.find((type) => MediaRecorder.isTypeSupported(type));
        recordedChunks = [];
        if (recordingUrl.value)
            URL.revokeObjectURL(recordingUrl.value);
        recordingUrl.value = '';
        mediaRecorder = new MediaRecorder(mediaStream, mimeType ? { mimeType } : undefined);
        mediaRecorder.ondataavailable = (event) => {
            if (event.data.size)
                recordedChunks.push(event.data);
        };
        mediaRecorder.onstop = () => {
            const blob = new Blob(recordedChunks, { type: mediaRecorder?.mimeType || 'video/webm' });
            recordingUrl.value = URL.createObjectURL(blob);
        };
        mediaRecorder.start(1000);
        recording.value = true;
    }
    catch {
        error.value = 'Záznam se na tomto zařízení nepodařilo spustit.';
    }
}
function stopLocalRecording() {
    if (!mediaRecorder || mediaRecorder.state === 'inactive')
        return;
    mediaRecorder.stop();
    recording.value = false;
}
function flashClap() {
    window.clearTimeout(flashTimer);
    flashVisible.value = true;
    flashTimer = window.setTimeout(() => (flashVisible.value = false), 700);
}
function roleLabel(value) {
    if (value === 'director')
        return 'režisér';
    if (value === 'main_camera')
        return 'hlavní kamera';
    return value === 'top_camera' ? 'top-over kamera' : 'vedlejší kamera';
}
onBeforeUnmount(() => {
    socket?.close();
    stopLocalRecording();
    mediaStream?.getTracks().forEach((track) => track.stop());
    if (recordingUrl.value)
        URL.revokeObjectURL(recordingUrl.value);
    window.clearTimeout(flashTimer);
});
const __VLS_ctx = {
    ...{},
    ...{},
};
let __VLS_components;
let __VLS_intrinsics;
let __VLS_directives;
__VLS_asFunctionalElement1(__VLS_intrinsics.main, __VLS_intrinsics.main)({});
__VLS_asFunctionalElement1(__VLS_intrinsics.header, __VLS_intrinsics.header)({});
__VLS_asFunctionalElement1(__VLS_intrinsics.span, __VLS_intrinsics.span)({
    ...{ class: "eyebrow" },
});
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
__VLS_asFunctionalElement1(__VLS_intrinsics.h1, __VLS_intrinsics.h1)({});
if (!__VLS_ctx.role) {
    __VLS_asFunctionalElement1(__VLS_intrinsics.section, __VLS_intrinsics.section)({
        ...{ class: "card role-picker" },
    });
    /** @type {__VLS_StyleScopedClasses['card']} */ ;
    /** @type {__VLS_StyleScopedClasses['role-picker']} */ ;
    __VLS_asFunctionalElement1(__VLS_intrinsics.h2, __VLS_intrinsics.h2)({});
    __VLS_asFunctionalElement1(__VLS_intrinsics.button, __VLS_intrinsics.button)({
        ...{ onClick: (...[$event]) => {
                if (!(!__VLS_ctx.role))
                    throw 0;
                return (__VLS_ctx.role = 'director');
                // @ts-ignore
                [role, role,];
            } },
    });
    __VLS_asFunctionalElement1(__VLS_intrinsics.button, __VLS_intrinsics.button)({
        ...{ onClick: (...[$event]) => {
                if (!(!__VLS_ctx.role))
                    throw 0;
                return (__VLS_ctx.role = 'main_camera');
                // @ts-ignore
                [role,];
            } },
        ...{ class: "secondary" },
    });
    /** @type {__VLS_StyleScopedClasses['secondary']} */ ;
    __VLS_asFunctionalElement1(__VLS_intrinsics.button, __VLS_intrinsics.button)({
        ...{ onClick: (...[$event]) => {
                if (!(!__VLS_ctx.role))
                    throw 0;
                return (__VLS_ctx.role = 'top_camera');
                // @ts-ignore
                [role,];
            } },
        ...{ class: "secondary" },
    });
    /** @type {__VLS_StyleScopedClasses['secondary']} */ ;
    __VLS_asFunctionalElement1(__VLS_intrinsics.button, __VLS_intrinsics.button)({
        ...{ onClick: (...[$event]) => {
                if (!(!__VLS_ctx.role))
                    throw 0;
                return (__VLS_ctx.role = 'secondary_camera');
                // @ts-ignore
                [role,];
            } },
        ...{ class: "secondary" },
    });
    /** @type {__VLS_StyleScopedClasses['secondary']} */ ;
}
else if (!__VLS_ctx.session) {
    __VLS_asFunctionalElement1(__VLS_intrinsics.section, __VLS_intrinsics.section)({
        ...{ class: "card" },
    });
    /** @type {__VLS_StyleScopedClasses['card']} */ ;
    __VLS_asFunctionalElement1(__VLS_intrinsics.button, __VLS_intrinsics.button)({
        ...{ onClick: (...[$event]) => {
                if (!!(!__VLS_ctx.role))
                    throw 0;
                if (!(!__VLS_ctx.session))
                    throw 0;
                return (__VLS_ctx.role = null);
                // @ts-ignore
                [role, session,];
            } },
        ...{ class: "back" },
    });
    /** @type {__VLS_StyleScopedClasses['back']} */ ;
    if (__VLS_ctx.role === 'director') {
        __VLS_asFunctionalElement1(__VLS_intrinsics.h2, __VLS_intrinsics.h2)({});
        __VLS_asFunctionalElement1(__VLS_intrinsics.label, __VLS_intrinsics.label)({});
        __VLS_asFunctionalElement1(__VLS_intrinsics.input)({});
        (__VLS_ctx.sessionName);
        __VLS_asFunctionalElement1(__VLS_intrinsics.button, __VLS_intrinsics.button)({
            ...{ onClick: (__VLS_ctx.startDirector) },
            disabled: (__VLS_ctx.busy),
        });
    }
    else {
        __VLS_asFunctionalElement1(__VLS_intrinsics.h2, __VLS_intrinsics.h2)({});
        (__VLS_ctx.roleLabel(__VLS_ctx.role));
        __VLS_asFunctionalElement1(__VLS_intrinsics.label, __VLS_intrinsics.label)({});
        __VLS_asFunctionalElement1(__VLS_intrinsics.input)({});
        (__VLS_ctx.deviceName);
        __VLS_asFunctionalElement1(__VLS_intrinsics.p, __VLS_intrinsics.p)({
            ...{ class: "muted" },
        });
        /** @type {__VLS_StyleScopedClasses['muted']} */ ;
        __VLS_asFunctionalElement1(__VLS_intrinsics.button, __VLS_intrinsics.button)({
            ...{ onClick: (__VLS_ctx.joinCamera) },
            disabled: (__VLS_ctx.busy || !__VLS_ctx.deviceName),
        });
    }
}
else {
    __VLS_asFunctionalElement1(__VLS_intrinsics.section, __VLS_intrinsics.section)({
        ...{ class: "card" },
    });
    /** @type {__VLS_StyleScopedClasses['card']} */ ;
    __VLS_asFunctionalElement1(__VLS_intrinsics.div, __VLS_intrinsics.div)({
        ...{ class: "session-heading" },
    });
    /** @type {__VLS_StyleScopedClasses['session-heading']} */ ;
    __VLS_asFunctionalElement1(__VLS_intrinsics.div, __VLS_intrinsics.div)({});
    __VLS_asFunctionalElement1(__VLS_intrinsics.span, __VLS_intrinsics.span)({
        ...{ class: "eyebrow" },
    });
    /** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
    (__VLS_ctx.roleLabel(__VLS_ctx.role));
    __VLS_asFunctionalElement1(__VLS_intrinsics.h2, __VLS_intrinsics.h2)({});
    (__VLS_ctx.session.name);
    __VLS_asFunctionalElement1(__VLS_intrinsics.span, __VLS_intrinsics.span)({
        ...{ class: "status" },
    });
    /** @type {__VLS_StyleScopedClasses['status']} */ ;
    (__VLS_ctx.session.state);
    if (__VLS_ctx.role === 'director') {
        __VLS_asFunctionalElement1(__VLS_intrinsics.h3, __VLS_intrinsics.h3)({});
        (__VLS_ctx.devices.length);
        __VLS_asFunctionalElement1(__VLS_intrinsics.div, __VLS_intrinsics.div)({
            ...{ class: "record-controls" },
        });
        /** @type {__VLS_StyleScopedClasses['record-controls']} */ ;
        if (__VLS_ctx.session.state !== 'recording') {
            __VLS_asFunctionalElement1(__VLS_intrinsics.button, __VLS_intrinsics.button)({
                ...{ onClick: (...[$event]) => {
                        if (!!(!__VLS_ctx.role))
                            throw 0;
                        if (!!(!__VLS_ctx.session))
                            throw 0;
                        if (!(__VLS_ctx.role === 'director'))
                            throw 0;
                        if (!(__VLS_ctx.session.state !== 'recording'))
                            throw 0;
                        return (__VLS_ctx.sendRecordingCommand('recording.start'));
                        // @ts-ignore
                        [role, role, role, role, session, session, session, sessionName, startDirector, busy, busy, roleLabel, roleLabel, deviceName, deviceName, joinCamera, devices, sendRecordingCommand,];
                    } },
                disabled: (!__VLS_ctx.devices.length),
            });
        }
        else {
            __VLS_asFunctionalElement1(__VLS_intrinsics.button, __VLS_intrinsics.button)({
                ...{ onClick: (...[$event]) => {
                        if (!!(!__VLS_ctx.role))
                            throw 0;
                        if (!!(!__VLS_ctx.session))
                            throw 0;
                        if (!(__VLS_ctx.role === 'director'))
                            throw 0;
                        if (!!(__VLS_ctx.session.state !== 'recording'))
                            throw 0;
                        return (__VLS_ctx.sendRecordingCommand('recording.stop'));
                        // @ts-ignore
                        [devices, sendRecordingCommand,];
                    } },
                ...{ class: "stop" },
            });
            /** @type {__VLS_StyleScopedClasses['stop']} */ ;
        }
        __VLS_asFunctionalElement1(__VLS_intrinsics.button, __VLS_intrinsics.button)({
            ...{ onClick: (__VLS_ctx.triggerClap) },
            ...{ class: "clap-button" },
            disabled: (!__VLS_ctx.devices.some(device => device.role === 'main_camera' && device.connected)),
        });
        /** @type {__VLS_StyleScopedClasses['clap-button']} */ ;
        if (!__VLS_ctx.devices.length) {
            __VLS_asFunctionalElement1(__VLS_intrinsics.p, __VLS_intrinsics.p)({
                ...{ class: "muted" },
            });
            /** @type {__VLS_StyleScopedClasses['muted']} */ ;
        }
        for (const [device] of __VLS_vFor((__VLS_ctx.devices))) {
            __VLS_asFunctionalElement1(__VLS_intrinsics.article, __VLS_intrinsics.article)({
                key: (device.device_id),
                ...{ class: "device" },
            });
            /** @type {__VLS_StyleScopedClasses['device']} */ ;
            __VLS_asFunctionalElement1(__VLS_intrinsics.div, __VLS_intrinsics.div)({});
            __VLS_asFunctionalElement1(__VLS_intrinsics.strong, __VLS_intrinsics.strong)({});
            (device.name);
            (__VLS_ctx.roleLabel(device.role));
            __VLS_asFunctionalElement1(__VLS_intrinsics.small, __VLS_intrinsics.small)({});
            (device.capabilities.battery_percent ?? 'neznámá');
            (__VLS_ctx.formatBytes(device.capabilities.free_storage_bytes));
            __VLS_asFunctionalElement1(__VLS_intrinsics.small, __VLS_intrinsics.small)({});
            (__VLS_ctx.permissionLabel(device.capabilities.camera_permission));
            (__VLS_ctx.permissionLabel(device.capabilities.microphone_permission));
            __VLS_asFunctionalElement1(__VLS_intrinsics.span, __VLS_intrinsics.span)({
                ...{ class: (['dot', { offline: !device.connected }]) },
            });
            /** @type {__VLS_StyleScopedClasses['offline']} */ ;
            /** @type {__VLS_StyleScopedClasses['dot']} */ ;
            (device.connected ? device.state : 'odpojeno');
            // @ts-ignore
            [roleLabel, devices, devices, devices, triggerClap, formatBytes, permissionLabel, permissionLabel,];
        }
    }
    else {
        __VLS_asFunctionalElement1(__VLS_intrinsics.video, __VLS_intrinsics.video)({
            ref: "preview",
            ...{ class: "preview" },
            autoplay: true,
            muted: true,
            playsinline: true,
        });
        /** @type {__VLS_StyleScopedClasses['preview']} */ ;
        __VLS_asFunctionalElement1(__VLS_intrinsics.div, __VLS_intrinsics.div)({
            ...{ class: "ready" },
        });
        /** @type {__VLS_StyleScopedClasses['ready']} */ ;
        __VLS_asFunctionalElement1(__VLS_intrinsics.span, __VLS_intrinsics.span)({});
        __VLS_asFunctionalElement1(__VLS_intrinsics.div, __VLS_intrinsics.div)({});
        __VLS_asFunctionalElement1(__VLS_intrinsics.strong, __VLS_intrinsics.strong)({});
        __VLS_asFunctionalElement1(__VLS_intrinsics.small, __VLS_intrinsics.small)({});
        (__VLS_ctx.deviceName);
        if (__VLS_ctx.role === 'main_camera') {
            __VLS_asFunctionalElement1(__VLS_intrinsics.div, __VLS_intrinsics.div)({
                ...{ class: "record-controls" },
            });
            /** @type {__VLS_StyleScopedClasses['record-controls']} */ ;
            if (__VLS_ctx.session.state !== 'recording') {
                __VLS_asFunctionalElement1(__VLS_intrinsics.button, __VLS_intrinsics.button)({
                    ...{ onClick: (...[$event]) => {
                            if (!!(!__VLS_ctx.role))
                                throw 0;
                            if (!!(!__VLS_ctx.session))
                                throw 0;
                            if (!!(__VLS_ctx.role === 'director'))
                                throw 0;
                            if (!(__VLS_ctx.role === 'main_camera'))
                                throw 0;
                            if (!(__VLS_ctx.session.state !== 'recording'))
                                throw 0;
                            return (__VLS_ctx.sendRecordingCommand('recording.start'));
                            // @ts-ignore
                            [role, session, deviceName, sendRecordingCommand,];
                        } },
                    disabled: (!__VLS_ctx.cameraReady),
                });
            }
            else {
                __VLS_asFunctionalElement1(__VLS_intrinsics.button, __VLS_intrinsics.button)({
                    ...{ onClick: (...[$event]) => {
                            if (!!(!__VLS_ctx.role))
                                throw 0;
                            if (!!(!__VLS_ctx.session))
                                throw 0;
                            if (!!(__VLS_ctx.role === 'director'))
                                throw 0;
                            if (!(__VLS_ctx.role === 'main_camera'))
                                throw 0;
                            if (!!(__VLS_ctx.session.state !== 'recording'))
                                throw 0;
                            return (__VLS_ctx.sendRecordingCommand('recording.stop'));
                            // @ts-ignore
                            [sendRecordingCommand, cameraReady,];
                        } },
                    ...{ class: "stop" },
                });
                /** @type {__VLS_StyleScopedClasses['stop']} */ ;
            }
        }
        else if (__VLS_ctx.recording) {
            __VLS_asFunctionalElement1(__VLS_intrinsics.p, __VLS_intrinsics.p)({
                ...{ class: "recording-indicator" },
            });
            /** @type {__VLS_StyleScopedClasses['recording-indicator']} */ ;
        }
        if (__VLS_ctx.role === 'main_camera') {
            __VLS_asFunctionalElement1(__VLS_intrinsics.button, __VLS_intrinsics.button)({
                ...{ onClick: (__VLS_ctx.flashClap) },
                ...{ class: "clap-button" },
            });
            /** @type {__VLS_StyleScopedClasses['clap-button']} */ ;
        }
        if (__VLS_ctx.recordingUrl) {
            __VLS_asFunctionalElement1(__VLS_intrinsics.a, __VLS_intrinsics.a)({
                ...{ class: "download" },
                href: (__VLS_ctx.recordingUrl),
                download: (`${__VLS_ctx.role}-${Date.now()}.webm`),
            });
            /** @type {__VLS_StyleScopedClasses['download']} */ ;
        }
    }
}
if (__VLS_ctx.error) {
    __VLS_asFunctionalElement1(__VLS_intrinsics.p, __VLS_intrinsics.p)({
        ...{ class: "error" },
    });
    /** @type {__VLS_StyleScopedClasses['error']} */ ;
    (__VLS_ctx.error);
}
if (__VLS_ctx.flashVisible) {
    __VLS_asFunctionalElement1(__VLS_intrinsics.div, __VLS_intrinsics.div)({
        ...{ class: "flash" },
        'aria-label': "Světelná klapka",
    });
    /** @type {__VLS_StyleScopedClasses['flash']} */ ;
}
// @ts-ignore
[role, role, recording, flashClap, recordingUrl, recordingUrl, error, error, flashVisible,];
const __VLS_export = (await import('vue')).defineComponent({});
export default {};
