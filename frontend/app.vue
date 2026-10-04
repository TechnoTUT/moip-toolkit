<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'

const colorMode = useColorMode()
function toggleColorMode() {
  colorMode.preference = colorMode.value === 'dark' ? 'light' : 'dark'
}

// Types
interface NDISource {
  name: string
  stream_name: string | null
  host_name: string | null
}

interface VideoDevice {
  index: number
  name: string
  is_available: boolean
}

interface AudioDevice {
  index: number
  name: string
  max_input_channels: number
  default_samplerate: number
}

interface RxStatus {
  running: boolean
  is_connected: boolean
  current_source: string | null
  recv_fmt: string
  recv_bandwidth: string
  fullscreen: boolean
  width: number
  height: number
  fps: number
  fps_real?: number
  dropped_frames?: number
  error: string | null
}

interface TxStatus {
  running: boolean
  sender_name: string | null
  video_device: number | null
  audio_device: number | null
  no_audio: boolean
  actual_width: number
  actual_height: number
  actual_fps: number
  fps_real?: number
  sample_rate: number
  audio_channels: number
  audio_level_l?: number
  audio_level_r?: number
  audio_peak_l?: number
  audio_peak_r?: number
  error: string | null
}

interface SystemStatus {
  cpu_percent: number
  mem_percent: number
  mem_used_mb: number
  mem_total_mb: number
  load_avg: number[]
}

interface AppSettings {
  rx: {
    auto_start: boolean
    sender_name: string | null
    recv_fmt: string
    recv_bandwidth: string
    fullscreen: boolean
  }
  tx: {
    auto_start: boolean
    sender_name: string
    video_device: number
    audio_device: number | null
    no_audio: boolean
    x_res: number
    y_res: number
    fps: string
    pix_fmt: string
  }
}

interface MultiviewStatus {
  running: boolean
  fullscreen: boolean
  slots: Array<{
    source_name: string
    is_connected: boolean
    width: number
    height: number
    audio_level_l: number
    audio_level_r: number
  }>
  error: string | null
}

// Active Tab
const activeTab = ref<'rx' | 'tx' | 'multiview'>('rx')

// State
const ndiSources = ref<NDISource[]>([])
const videoDevices = ref<VideoDevice[]>([])
const audioDevices = ref<AudioDevice[]>([])

const multiviewStatus = ref<MultiviewStatus>({
  running: false,
  fullscreen: false,
  slots: [],
  error: null
})
const multiviewFullscreen = ref(false)
const multiviewLoading = ref(false)

const appSettings = ref<AppSettings>({
  rx: {
    auto_start: false,
    sender_name: null,
    recv_fmt: 'rgb',
    recv_bandwidth: 'highest',
    fullscreen: false
  },
  tx: {
    auto_start: false,
    sender_name: 'TX',
    video_device: 0,
    audio_device: null,
    no_audio: false,
    x_res: 1920,
    y_res: 1080,
    fps: '30',
    pix_fmt: 'BGRX'
  }
})

const rxStatus = ref<RxStatus>({
  running: false,
  is_connected: false,
  current_source: null,
  recv_fmt: 'rgb',
  recv_bandwidth: 'highest',
  fullscreen: false,
  width: 0,
  height: 0,
  fps: 0,
  fps_real: 0,
  audio_level_l: -60,
  audio_level_r: -60,
  audio_peak_l: -60,
  audio_peak_r: -60,
  error: null
})

const txStatus = ref<TxStatus>({
  running: false,
  sender_name: null,
  video_device: null,
  audio_device: null,
  no_audio: false,
  actual_width: 0,
  actual_height: 0,
  actual_fps: 0,
  sample_rate: 0,
  audio_channels: 0,
  audio_level_l: -60,
  audio_level_r: -60,
  audio_peak_l: -60,
  audio_peak_r: -60,
  error: null
})

const systemStatus = ref<SystemStatus>({
  cpu_percent: 0,
  mem_percent: 0,
  mem_used_mb: 0,
  mem_total_mb: 0,
  load_avg: [0, 0, 0]
})

// Form Options
const rxFullscreen = ref(false)
const rxFmt = ref('rgb')
const rxBandwidth = ref('highest')
const rxAutoStart = ref(false)

const txSenderName = ref('TX')
const txVideoDevice = ref(0)
const txAudioDevice = ref<number | null>(null)
const txNoAudio = ref(false)
const txResolution = ref('1920x1080')
const txFps = ref('30')
const txPixFmt = ref('BGRX')
const txAutoStart = ref(false)

// Loading states
const rxLoading = ref(false)
const txLoading = ref(false)

// Web Preview State
const previewSource = ref<string | null>(null)
const previewKey = ref(0)
const isPreviewModalOpen = ref(false)
const previewMode = ref<'webrtc' | 'mjpeg'>('webrtc')
const webrtcVideoRef = ref<HTMLVideoElement | null>(null)
let webrtcPc: RTCPeerConnection | null = null

async function initWebRTC(sourceName: string) {
  try {
    if (webrtcPc) {
      webrtcPc.close()
      webrtcPc = null
    }

    const pc = new RTCPeerConnection({
      iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
    })
    webrtcPc = pc

    pc.addTransceiver('video', { direction: 'recvonly' })

    pc.ontrack = (event) => {
      if (webrtcVideoRef.value && event.streams && event.streams[0]) {
        webrtcVideoRef.value.srcObject = event.streams[0]
      }
    }

    const offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    const res = await fetch(`${API_BASE}/api/webrtc/offer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source: sourceName,
        sdp: pc.localDescription?.sdp,
        type: pc.localDescription?.type
      })
    })

    if (!res.ok) {
      throw new Error(`Signaling failed: ${res.statusText}`)
    }

    const answer = await res.json()
    await pc.setRemoteDescription(new RTCSessionDescription(answer))
    previewMode.value = 'webrtc'
  } catch (err) {
    console.warn('WebRTC connection failed, falling back to MJPEG:', err)
    previewMode.value = 'mjpeg'
  }
}

function openPreview(sourceName: string) {
  previewSource.value = sourceName
  previewKey.value = Date.now()
  isPreviewModalOpen.value = true
  initWebRTC(sourceName)
}

function closePreview() {
  isPreviewModalOpen.value = false
  previewSource.value = null
  if (webrtcPc) {
    webrtcPc.close()
    webrtcPc = null
  }
  if (webrtcVideoRef.value) {
    webrtcVideoRef.value.srcObject = null
  }
}

// API Base
const API_BASE = ''

// Fetchers
async function fetchSources() {
  try {
    const res = await fetch(`${API_BASE}/api/ndi/sources`)
    if (res.ok) {
      ndiSources.value = await res.json()
    }
  } catch (e) {
    console.error('Failed to fetch NDI sources', e)
  }
}

async function fetchDevices() {
  try {
    const [vRes, aRes] = await Promise.all([
      fetch(`${API_BASE}/api/devices/video`),
      fetch(`${API_BASE}/api/devices/audio`)
    ])
    if (vRes.ok) videoDevices.value = await vRes.json()
    if (aRes.ok) {
      const devs = await aRes.json()
      audioDevices.value = devs
      if (devs.length > 0 && txAudioDevice.value === null) {
        txAudioDevice.value = devs[0].index
      }
    }
  } catch (e) {
    console.error('Failed to fetch devices', e)
  }
}

async function fetchStatus() {
  try {
    const [rRes, tRes] = await Promise.all([
      fetch(`${API_BASE}/api/rx/status`),
      fetch(`${API_BASE}/api/tx/status`)
    ])
    if (rRes.ok) rxStatus.value = await rRes.json()
    if (tRes.ok) txStatus.value = await tRes.json()
  } catch (e) {
    console.error('Failed to fetch status', e)
  }
}

// RX Actions
async function startRx(sourceName: string) {
  rxLoading.value = true
  try {
    const res = await fetch(`${API_BASE}/api/rx/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sender_name: sourceName,
        recv_fmt: rxFmt.value,
        recv_bandwidth: rxBandwidth.value,
        fullscreen: rxFullscreen.value
      })
    })
    if (res.ok) rxStatus.value = await res.json()
  } catch (e) {
    console.error('Failed to start RX', e)
  } finally {
    rxLoading.value = false
  }
}

async function switchRx(sourceName: string) {
  rxLoading.value = true
  try {
    const res = await fetch(`${API_BASE}/api/rx/switch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sender_name: sourceName })
    })
    if (res.ok) rxStatus.value = await res.json()
  } catch (e) {
    console.error('Failed to switch RX source', e)
  } finally {
    rxLoading.value = false
  }
}

async function stopRx() {
  rxLoading.value = true
  try {
    const res = await fetch(`${API_BASE}/api/rx/stop`, { method: 'POST' })
    if (res.ok) {
      const data = await res.json()
      lastRxJson = JSON.stringify(data)
      rxStatus.value = data
    }
  } catch (e) {
    console.error('Failed to stop RX', e)
  } finally {
    rxLoading.value = false
  }
}

// TX Actions
async function startTx() {
  txLoading.value = true
  try {
    const [x, y] = txResolution.value.split('x').map(Number)
    const res = await fetch(`${API_BASE}/api/tx/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        video_device: txVideoDevice.value,
        audio_device: txNoAudio.value ? null : txAudioDevice.value,
        no_audio: txNoAudio.value,
        sender_name: txSenderName.value,
        x_res: x,
        y_res: y,
        fps: txFps.value,
        pix_fmt: txPixFmt.value,
        sample_rate: 48000,
        audio_channels: 2
      })
    })
    if (res.ok) txStatus.value = await res.json()
  } catch (e) {
    console.error('Failed to start TX', e)
  } finally {
    txLoading.value = false
  }
}

async function stopTx() {
  txLoading.value = true
  try {
    const res = await fetch(`${API_BASE}/api/tx/stop`, { method: 'POST' })
    if (res.ok) {
      const data = await res.json()
      lastTxJson = JSON.stringify(data)
      txStatus.value = data
    }
  } catch (e) {
    console.error('Failed to stop TX', e)
  } finally {
    txLoading.value = false
  }
}

// Multi-Viewer (SDL2 Window) Actions
async function startMultiview() {
  multiviewLoading.value = true
  try {
    const res = await fetch(`${API_BASE}/api/multiview/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sources: null, // Auto-discover all sources
        fullscreen: multiviewFullscreen.value
      })
    })
    if (res.ok) {
      multiviewStatus.value = await res.json()
    }
  } catch (e) {
    console.error('Failed to start Multiview', e)
  } finally {
    multiviewLoading.value = false
  }
}

async function stopMultiview() {
  multiviewLoading.value = true
  try {
    const res = await fetch(`${API_BASE}/api/multiview/stop`, { method: 'POST' })
    if (res.ok) {
      multiviewStatus.value = await res.json()
    }
  } catch (e) {
    console.error('Failed to stop Multiview', e)
  } finally {
    multiviewLoading.value = false
  }
}

let hasPopulatedFromSettings = false

async function fetchSettings() {
  try {
    const res = await fetch(`${API_BASE}/api/settings`)
    if (res.ok) {
      const data: AppSettings = await res.json()
      appSettings.value = data
      rxAutoStart.value = data.rx.auto_start
      txAutoStart.value = data.tx.auto_start

      if (!hasPopulatedFromSettings) {
        hasPopulatedFromSettings = true
        // Restore RX preset form options
        if (data.rx.recv_fmt) rxFmt.value = data.rx.recv_fmt
        if (data.rx.recv_bandwidth) rxBandwidth.value = data.rx.recv_bandwidth
        if (data.rx.fullscreen !== undefined) rxFullscreen.value = data.rx.fullscreen

        // Restore TX preset form options
        if (data.tx.sender_name) txSenderName.value = data.tx.sender_name
        if (data.tx.video_device !== undefined) txVideoDevice.value = data.tx.video_device
        if (data.tx.audio_device !== undefined) txAudioDevice.value = data.tx.audio_device
        if (data.tx.no_audio !== undefined) txNoAudio.value = data.tx.no_audio
        if (data.tx.x_res && data.tx.y_res) txResolution.value = `${data.tx.x_res}x${data.tx.y_res}`
        if (data.tx.fps) txFps.value = data.tx.fps
        if (data.tx.pix_fmt) txPixFmt.value = data.tx.pix_fmt
      }
    }
  } catch (e) {
    console.error('Failed to fetch settings', e)
  }
}

async function toggleRxAutoStart() {
  const updated: AppSettings = {
    ...appSettings.value,
    rx: {
      ...appSettings.value.rx,
      auto_start: rxAutoStart.value,
      recv_fmt: rxFmt.value,
      recv_bandwidth: rxBandwidth.value,
      fullscreen: rxFullscreen.value
    }
  }
  await saveSettings(updated)
}

async function toggleTxAutoStart() {
  const [x, y] = txResolution.value.split('x').map(Number)
  const updated: AppSettings = {
    ...appSettings.value,
    tx: {
      ...appSettings.value.tx,
      auto_start: txAutoStart.value,
      sender_name: txSenderName.value,
      video_device: txVideoDevice.value,
      audio_device: txNoAudio.value ? null : txAudioDevice.value,
      no_audio: txNoAudio.value,
      x_res: x || 1920,
      y_res: y || 1080,
      fps: txFps.value,
      pix_fmt: txPixFmt.value
    }
  }
  await saveSettings(updated)
}

async function saveSettings(settings: AppSettings) {
  try {
    const res = await fetch(`${API_BASE}/api/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings)
    })
    if (res.ok) {
      appSettings.value = await res.json()
    }
  } catch (e) {
    console.error('Failed to save settings', e)
  }
}

let eventSource: EventSource | null = null
let lastSourcesJson = ''
let lastRxJson = ''
let lastTxJson = ''

function setupSSE() {
  if (eventSource) {
    eventSource.close()
  }

  eventSource = new EventSource(`${API_BASE}/api/events`)

  eventSource.addEventListener('state', (event) => {
    try {
      const data = JSON.parse(event.data)
      if (data.sources) {
        const sourcesJson = JSON.stringify(data.sources)
        if (sourcesJson !== lastSourcesJson) {
          lastSourcesJson = sourcesJson
          ndiSources.value = data.sources
        }
      }
      if (data.rx) {
        const rxJson = JSON.stringify(data.rx)
        if (rxJson !== lastRxJson) {
          lastRxJson = rxJson
          rxStatus.value = data.rx
        }
      }
      if (data.tx) {
        const txJson = JSON.stringify(data.tx)
        if (txJson !== lastTxJson) {
          lastTxJson = txJson
          txStatus.value = data.tx
        }
      }
      if (data.multiview) {
        multiviewStatus.value = data.multiview
      }
      if (data.system) {
        systemStatus.value = data.system
      }
      if (data.settings) {
        appSettings.value = data.settings
        rxAutoStart.value = data.settings.rx.auto_start
        txAutoStart.value = data.settings.tx.auto_start
      }
    } catch (e) {
      console.error('Failed to parse SSE state message', e)
    }
  })

  eventSource.onerror = (e) => {
    console.warn('SSE connection lost or error, will automatically retry...', e)
  }
}

onMounted(() => {
  fetchSettings()
  fetchDevices()
  setupSSE()
})

onUnmounted(() => {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
})
</script>

<template>
  <div class="min-h-screen bg-slate-50 dark:bg-slate-950 text-slate-800 dark:text-slate-100 flex flex-col font-sans transition-colors duration-200">
    <!-- Top Navigation Bar -->
    <header class="border-b border-slate-200 dark:border-slate-800 bg-white/90 dark:bg-slate-900/80 backdrop-blur px-6 py-3 flex items-center justify-between sticky top-0 z-30 shadow-sm">
      <div class="flex items-center gap-5">
        <a href="/" class="flex items-center gap-4 py-0.5">
          <!-- Light theme logo (#3a3a3a + #c7000a) matching technotut.net navbar-brand (height 48px / width 200px) -->
          <img src="/logo.svg" alt="TechnoTUT" class="h-12 w-auto max-w-[200px] object-contain dark:hidden" />
          <!-- Dark theme logo (#ffffff + #c7000a) -->
          <img src="/logo_dark.svg" alt="TechnoTUT" class="h-12 w-auto max-w-[200px] object-contain hidden dark:block" />
          <div class="h-8 w-px bg-slate-200 dark:bg-slate-800"></div>
          <div>
            <h1 class="text-base font-bold tracking-tight text-slate-900 dark:text-slate-100 flex items-center gap-2">
              <span>MoIP Toolkit</span>
            </h1>
            <p class="text-[11px] text-slate-500 dark:text-slate-400">The Utopia Tone Streaming Network</p>
          </div>
        </a>
      </div>

      <div class="flex items-center gap-3">
        <!-- Tab Switcher -->
        <div class="flex bg-slate-100 dark:bg-slate-900 p-1 rounded-xl border border-slate-200 dark:border-slate-800">
          <button
            @click="activeTab = 'rx'"
            :class="[
              'px-5 py-2 rounded-lg text-sm font-semibold transition flex items-center gap-2',
              activeTab === 'rx' ? 'bg-[#C7000A] text-white shadow-md shadow-[#C7000A]/20' : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-100'
            ]"
          >
            <span>NDI Decoder (RX)</span>
            <span
              class="h-2 w-2 rounded-full"
              :class="rxStatus.running ? (rxStatus.is_connected ? 'bg-emerald-400 animate-pulse' : 'bg-amber-400') : 'bg-slate-300 dark:bg-slate-600'"
            />
          </button>
          <button
            @click="activeTab = 'tx'"
            :class="[
              'px-5 py-2 rounded-lg text-sm font-semibold transition flex items-center gap-2',
              activeTab === 'tx' ? 'bg-[#C7000A] text-white shadow-md shadow-[#C7000A]/20' : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-100'
            ]"
          >
            <span>NDI Encoder (TX)</span>
            <span
              class="h-2 w-2 rounded-full"
              :class="txStatus.running ? 'bg-red-400 animate-pulse' : 'bg-slate-300 dark:bg-slate-600'"
            />
          </button>
          <button
            @click="activeTab = 'multiview'"
            :class="[
              'px-5 py-2 rounded-lg text-sm font-semibold transition flex items-center gap-2',
              activeTab === 'multiview' ? 'bg-[#C7000A] text-white shadow-md shadow-[#C7000A]/20' : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-100'
            ]"
          >
            <span>Multi-View</span>
            <span
              class="h-2 w-2 rounded-full"
              :class="multiviewStatus.running ? 'bg-purple-400 animate-pulse' : 'bg-slate-300 dark:bg-slate-600'"
            />
          </button>
        </div>

        <!-- System Stats Badges (Fixed width & tabular numbers to prevent layout jitter) -->
        <div class="hidden md:flex items-center gap-2 bg-slate-100 dark:bg-slate-900 px-3 py-1.5 rounded-xl border border-slate-200 dark:border-slate-800 text-xs font-mono select-none">
          <div class="flex items-center gap-1.5">
            <span class="text-slate-400 font-sans font-semibold text-[10px] uppercase">CPU</span>
            <span
              class="w-[42px] text-right font-bold tabular-nums"
              :class="[
                systemStatus.cpu_percent > 85 ? 'text-rose-500 animate-pulse' :
                systemStatus.cpu_percent > 60 ? 'text-amber-500' : 'text-emerald-500'
              ]"
            >
              {{ systemStatus.cpu_percent.toFixed(1) }}%
            </span>
          </div>

          <div class="w-px h-3 bg-slate-300 dark:bg-slate-700"></div>

          <div class="flex items-center gap-1.5">
            <span class="text-slate-400 font-sans font-semibold text-[10px] uppercase">RAM</span>
            <span
              class="w-[42px] text-right font-bold tabular-nums"
              :class="[
                systemStatus.mem_percent > 85 ? 'text-rose-500' :
                systemStatus.mem_percent > 70 ? 'text-amber-500' : 'text-slate-700 dark:text-slate-200'
              ]"
            >
              {{ systemStatus.mem_percent.toFixed(1) }}%
            </span>
          </div>

          <div class="w-px h-3 bg-slate-300 dark:bg-slate-700"></div>

          <div class="flex items-center gap-1 text-slate-500 dark:text-slate-400">
            <span class="font-sans font-semibold text-[10px] uppercase">Load</span>
            <span class="w-[32px] text-right tabular-nums">{{ (systemStatus.load_avg[0] || 0).toFixed(2) }}</span>
          </div>
        </div>

        <!-- Theme Toggle Button -->
        <button
          @click="toggleColorMode"
          class="p-2 rounded-xl border border-slate-200 dark:border-slate-800 text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100 hover:bg-slate-100 dark:hover:bg-slate-800 transition"
          title="Toggle Light / Dark Mode"
        >
          <!-- Sun icon for light mode -->
          <svg v-if="colorMode.value === 'dark'" class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
          </svg>
          <!-- Moon icon for dark mode -->
          <svg v-else class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
          </svg>
        </button>
      </div>
    </header>

    <!-- Main Content -->
    <main class="flex-1 p-6 max-w-7xl mx-auto w-full space-y-6">
      
      <!-- ==================== TAB: RX (Receiver) ==================== -->
      <div v-if="activeTab === 'rx'" class="grid grid-cols-1 lg:grid-cols-3 gap-6">
        
        <!-- Status & Settings Panel (Left 1 col) -->
        <div class="space-y-6 lg:col-span-1">
          <!-- Status Card -->
          <div class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-5 space-y-4 shadow-sm">
            <h2 class="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider">Viewer Status</h2>
            <div class="flex items-center justify-between">
              <span class="text-slate-600 dark:text-slate-300 font-medium text-sm">Status</span>
              <span
                class="px-2.5 py-1 rounded-full text-xs font-semibold uppercase tracking-wider"
                :class="rxStatus.running ? (rxStatus.is_connected ? 'bg-emerald-50 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-400 border border-emerald-200 dark:border-emerald-800' : 'bg-amber-50 dark:bg-amber-950/40 text-amber-700 dark:text-amber-400 border border-amber-200 dark:border-amber-800') : 'bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400'"
              >
                {{ rxStatus.running ? (rxStatus.is_connected ? 'Connected' : 'Searching') : 'Stopped' }}
              </span>
            </div>

            <div class="space-y-2 text-sm border-t border-slate-100 dark:border-slate-800 pt-3">
              <div class="flex justify-between">
                <span class="text-slate-500 dark:text-slate-400">Current Source:</span>
                <span class="text-slate-900 dark:text-slate-100 font-mono font-semibold truncate max-w-[180px]">{{ rxStatus.current_source || 'None' }}</span>
              </div>
              <div class="flex justify-between">
                <span class="text-slate-500 dark:text-slate-400">Resolution:</span>
                <span class="text-slate-900 dark:text-slate-100 font-mono font-medium">{{ rxStatus.width ? `${rxStatus.width} x ${rxStatus.height}` : '-' }}</span>
              </div>
              <div class="flex justify-between">
                <span class="text-slate-500 dark:text-slate-400">Render Rate:</span>
                <span class="text-slate-900 dark:text-slate-100 font-mono font-medium">
                  {{ rxStatus.running && rxStatus.is_connected ? `${rxStatus.fps_real?.toFixed(1) || '0.0'} fps` : '-' }}
                </span>
              </div>

              <!-- RX Audio Level Meter (VU Meter) -->
              <div v-if="rxStatus.running && rxStatus.is_connected" class="pt-2 border-t border-slate-100 dark:border-slate-800 space-y-2">
                <div class="flex justify-between items-center text-xs">
                  <span class="font-semibold text-slate-500 dark:text-slate-400">Audio Level (dBFS)</span>
                  <span class="font-mono text-slate-600 dark:text-slate-300">
                    {{ rxStatus.audio_level_l ?? -60 }} / {{ rxStatus.audio_level_r ?? -60 }} dB
                  </span>
                </div>

                <!-- Channel L -->
                <div class="space-y-1">
                  <div class="flex items-center gap-2">
                    <span class="text-[10px] font-mono text-slate-400 w-3">L</span>
                    <div class="flex-1 h-2 bg-slate-200 dark:bg-slate-800 rounded-full overflow-hidden flex">
                      <div
                        class="h-full transition-all duration-75 ease-out rounded-full"
                        :class="[
                          (rxStatus.audio_peak_l ?? -60) > -3 ? 'bg-rose-500' :
                          (rxStatus.audio_peak_l ?? -60) > -12 ? 'bg-amber-400' : 'bg-emerald-500'
                        ]"
                        :style="{ width: `${Math.max(0, Math.min(100, (((rxStatus.audio_level_l ?? -60) + 60) / 60) * 100))}%` }"
                      />
                    </div>
                  </div>

                  <!-- Channel R -->
                  <div class="flex items-center gap-2">
                    <span class="text-[10px] font-mono text-slate-400 w-3">R</span>
                    <div class="flex-1 h-2 bg-slate-200 dark:bg-slate-800 rounded-full overflow-hidden flex">
                      <div
                        class="h-full transition-all duration-75 ease-out rounded-full"
                        :class="[
                          (rxStatus.audio_peak_r ?? -60) > -3 ? 'bg-rose-500' :
                          (rxStatus.audio_peak_r ?? -60) > -12 ? 'bg-amber-400' : 'bg-emerald-500'
                        ]"
                        :style="{ width: `${Math.max(0, Math.min(100, (((rxStatus.audio_level_r ?? -60) + 60) / 60) * 100))}%` }"
                      />
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div v-if="rxStatus.running" class="pt-2">
              <button
                @click="stopRx"
                :disabled="rxLoading"
                class="w-full py-2.5 px-4 bg-rose-50 dark:bg-rose-950/30 hover:bg-rose-100 dark:hover:bg-rose-900/40 text-rose-700 dark:text-rose-400 border border-rose-200 dark:border-rose-800/60 rounded-xl font-medium text-sm transition"
              >
                Close Viewer Window
              </button>
            </div>
          </div>

          <!-- Launch Options -->
          <div class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-5 space-y-4 shadow-sm">
            <div class="flex items-center justify-between">
              <h2 class="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider">Viewer Options</h2>
              <span v-if="rxAutoStart" class="text-[10px] font-semibold text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-950/40 px-2 py-0.5 rounded-full border border-emerald-200 dark:border-emerald-800">
                Auto-Restore ON
              </span>
            </div>
            <div class="space-y-3">
              <label class="flex items-center justify-between cursor-pointer">
                <span class="text-sm font-medium text-slate-700 dark:text-slate-300">Fullscreen Window</span>
                <input type="checkbox" v-model="rxFullscreen" class="rounded bg-white dark:bg-slate-950 border-slate-300 dark:border-slate-700 text-[#C7000A] focus:ring-[#C7000A] h-4 w-4" />
              </label>

              <!-- Auto-Start on Boot / Reconnection -->
              <label class="flex items-center justify-between cursor-pointer pt-1 border-t border-slate-100 dark:border-slate-800">
                <div>
                  <span class="text-sm font-medium text-slate-700 dark:text-slate-300 block">Auto-Start on Boot</span>
                  <span class="text-xs text-slate-400 block">Resume last source when server starts</span>
                </div>
                <input
                  type="checkbox"
                  v-model="rxAutoStart"
                  @change="toggleRxAutoStart"
                  class="rounded bg-white dark:bg-slate-950 border-slate-300 dark:border-slate-700 text-[#C7000A] focus:ring-[#C7000A] h-4 w-4"
                />
              </label>

              <div>
                <label class="block text-xs font-medium text-slate-600 dark:text-slate-400 mb-1">Color Format</label>
                <select v-model="rxFmt" class="w-full bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-800 dark:text-slate-200 focus:outline-none focus:border-[#C7000A]">
                  <option value="rgb">RGB (Fast, default)</option>
                  <option value="bgr">BGR</option>
                  <option value="uyvy">UYVY (Lowest Bandwidth)</option>
                </select>
              </div>

              <div>
                <label class="block text-xs font-medium text-slate-600 dark:text-slate-400 mb-1">Bandwidth Mode</label>
                <select v-model="rxBandwidth" class="w-full bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-800 dark:text-slate-200 focus:outline-none focus:border-[#C7000A]">
                  <option value="highest">Highest Quality</option>
                  <option value="lowest">Lowest Latency / Proxy</option>
                </select>
              </div>
            </div>
          </div>
        </div>

        <!-- Available Sources List (Right 2 cols) -->
        <div class="lg:col-span-2 space-y-4">
          <div class="flex items-center justify-between">
            <h2 class="text-base font-bold text-slate-900 dark:text-slate-100 tracking-tight flex items-center gap-2">
              <span>Discovered NDI Sources</span>
              <span class="text-xs bg-slate-200 dark:bg-slate-800 text-slate-700 dark:text-slate-300 font-semibold px-2 py-0.5 rounded-full">{{ ndiSources.length }}</span>
            </h2>
            <button @click="fetchSources" class="text-xs font-medium text-slate-500 dark:text-slate-400 hover:text-[#C7000A] transition">
              Refresh Sources
            </button>
          </div>

          <div v-if="ndiSources.length === 0" class="bg-white dark:bg-slate-900 border border-dashed border-slate-300 dark:border-slate-800 rounded-2xl p-12 text-center space-y-2 shadow-sm">
            <p class="text-sm font-medium text-slate-600 dark:text-slate-400">No NDI sources detected on the local network.</p>
            <p class="text-xs text-slate-400 dark:text-slate-500">Ensure NDI senders (OBS, vMix, cameras, or TX) are active on the same subnet.</p>
          </div>

          <div v-else class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div
              v-for="source in ndiSources"
              :key="source.name"
              class="bg-white dark:bg-slate-900 border rounded-2xl p-5 flex flex-col justify-between space-y-4 transition shadow-sm"
              :class="rxStatus.current_source === source.name && rxStatus.running ? 'border-[#C7000A] bg-red-50/40 dark:bg-[#C7000A]/10 ring-2 ring-[#C7000A]/30' : 'border-slate-200 dark:border-slate-800 hover:border-slate-300 dark:hover:border-slate-700'"
            >
              <div>
                <div class="flex items-center justify-between mb-1.5">
                  <span class="text-xs font-mono text-[#C7000A] font-bold tracking-wide uppercase">{{ source.host_name || 'Host' }}</span>
                  <span v-if="rxStatus.current_source === source.name && rxStatus.running" class="px-2 py-0.5 bg-[#C7000A] text-white text-[11px] rounded-full font-bold">Active</span>
                </div>
                <h3 class="text-base font-bold text-slate-900 dark:text-slate-100 break-all">{{ source.name }}</h3>
                <p v-if="source.stream_name" class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">Stream: {{ source.stream_name }}</p>

                <!-- Live Thumbnail (WebRTC 24fps) -->
                <div
                  @click="openPreview(source.name)"
                  class="mt-3 relative aspect-video bg-black rounded-xl overflow-hidden cursor-pointer group border border-slate-200 dark:border-slate-800 flex items-center justify-center select-none"
                  title="Click to enlarge"
                >
                  <NdiPreviewPlayer
                    :source="source.name"
                    :fps="24"
                    :width="360"
                    :api-base="API_BASE"
                  />
                  <!-- Hover overlay indicating click to enlarge -->
                  <div class="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center gap-1.5 text-white text-xs font-semibold z-10">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" />
                    </svg>
                    <span>Click to Enlarge</span>
                  </div>
                  <!-- FPS badge -->
                  <span class="absolute bottom-1.5 right-1.5 px-1.5 py-0.5 rounded bg-black/60 backdrop-blur text-[10px] text-white/90 font-mono z-10">
                    24fps WebRTC
                  </span>
                </div>
              </div>

              <div>
                <div class="grid grid-cols-2 gap-2 mb-2">
                  <button
                    @click="openPreview(source.name)"
                    class="py-2 px-3 bg-slate-100 dark:bg-slate-800 hover:bg-slate-200 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-200 rounded-xl text-xs font-semibold flex items-center justify-center gap-1.5 transition"
                  >
                    <svg class="w-4 h-4 text-[#C7000A]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
                    </svg>
                    <span>Web Preview</span>
                  </button>
                  <button
                    v-if="rxStatus.running && rxStatus.current_source === source.name"
                    disabled
                    class="py-2 px-3 bg-[#C7000A]/10 text-[#C7000A] border border-[#C7000A]/30 rounded-xl text-xs font-semibold"
                  >
                    Displaying
                  </button>
                  <button
                    v-else-if="rxStatus.running"
                    @click="switchRx(source.name)"
                    :disabled="rxLoading"
                    class="py-2 px-3 bg-slate-100 dark:bg-slate-800 hover:bg-slate-200 dark:hover:bg-slate-700 text-slate-800 dark:text-slate-100 rounded-xl text-xs font-semibold transition"
                  >
                    Switch Window
                  </button>
                  <button
                    v-else
                    @click="startRx(source.name)"
                    :disabled="rxLoading"
                    class="py-2 px-3 bg-[#C7000A] hover:bg-[#b00009] text-white rounded-xl text-xs font-semibold transition shadow-md shadow-[#C7000A]/20"
                  >
                    Open Window
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>

      </div>


      <!-- ==================== TAB: TX (Sender) ==================== -->
      <div v-if="activeTab === 'tx'" class="grid grid-cols-1 lg:grid-cols-3 gap-6">
        
        <!-- Status Card (Left 1 col) -->
        <div class="space-y-6 lg:col-span-1">
          <div class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-5 space-y-4 shadow-sm">
            <h2 class="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider">Sender Status</h2>
            <div class="flex items-center justify-between">
              <span class="text-slate-600 dark:text-slate-300 font-medium text-sm">Status</span>
              <span
                class="px-2.5 py-1 rounded-full text-xs font-semibold uppercase tracking-wider"
                :class="txStatus.running ? 'bg-red-50 dark:bg-red-950/40 text-red-700 dark:text-red-400 border border-red-200 dark:border-red-800' : 'bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400'"
              >
                {{ txStatus.running ? 'Broadcasting' : 'Stopped' }}
              </span>
            </div>

            <div class="space-y-2 text-sm border-t border-slate-100 dark:border-slate-800 pt-3">
              <div class="flex justify-between">
                <span class="text-slate-500 dark:text-slate-400">Stream Name:</span>
                <span class="text-slate-900 dark:text-slate-100 font-mono font-semibold">{{ txStatus.sender_name || '-' }}</span>
              </div>
              <div class="flex justify-between">
                <span class="text-slate-500 dark:text-slate-400">Actual Output:</span>
                <span class="text-slate-900 dark:text-slate-100 font-mono font-medium">
                  {{ txStatus.actual_width ? `${txStatus.actual_width}x${txStatus.actual_height} @ ${txStatus.actual_fps.toFixed(1)}fps` : '-' }}
                </span>
              </div>
              <div class="flex justify-between">
                <span class="text-slate-500 dark:text-slate-400">Transmit Rate:</span>
                <span class="text-slate-900 dark:text-slate-100 font-mono font-medium">
                  {{ txStatus.running ? `${txStatus.fps_real?.toFixed(1) || '0.0'} fps` : '-' }}
                </span>
              </div>
              <div class="flex justify-between">
                <span class="text-slate-500 dark:text-slate-400">Audio:</span>
                <span class="text-slate-800 dark:text-slate-200">
                  {{ txStatus.no_audio ? 'Disabled' : (txStatus.running ? `${txStatus.sample_rate}Hz (${txStatus.audio_channels}ch)` : '-') }}
                </span>
              </div>

              <!-- Audio Level Meter (VU Meter) -->
              <div v-if="txStatus.running && !txStatus.no_audio" class="pt-2 border-t border-slate-100 dark:border-slate-800 space-y-2">
                <div class="flex justify-between items-center text-xs">
                  <span class="font-semibold text-slate-500 dark:text-slate-400">Audio Level (dBFS)</span>
                  <span class="font-mono text-slate-600 dark:text-slate-300">
                    {{ txStatus.audio_level_l ?? -60 }} / {{ txStatus.audio_level_r ?? -60 }} dB
                  </span>
                </div>

                <!-- Channel L -->
                <div class="space-y-1">
                  <div class="flex items-center gap-2">
                    <span class="text-[10px] font-mono text-slate-400 w-3">L</span>
                    <div class="flex-1 h-2 bg-slate-200 dark:bg-slate-800 rounded-full overflow-hidden flex">
                      <div
                        class="h-full transition-all duration-75 ease-out rounded-full"
                        :class="[
                          (txStatus.audio_peak_l ?? -60) > -3 ? 'bg-rose-500' :
                          (txStatus.audio_peak_l ?? -60) > -12 ? 'bg-amber-400' : 'bg-emerald-500'
                        ]"
                        :style="{ width: `${Math.max(0, Math.min(100, (((txStatus.audio_level_l ?? -60) + 60) / 60) * 100))}%` }"
                      />
                    </div>
                  </div>

                  <!-- Channel R -->
                  <div class="flex items-center gap-2">
                    <span class="text-[10px] font-mono text-slate-400 w-3">R</span>
                    <div class="flex-1 h-2 bg-slate-200 dark:bg-slate-800 rounded-full overflow-hidden flex">
                      <div
                        class="h-full transition-all duration-75 ease-out rounded-full"
                        :class="[
                          (txStatus.audio_peak_r ?? -60) > -3 ? 'bg-rose-500' :
                          (txStatus.audio_peak_r ?? -60) > -12 ? 'bg-amber-400' : 'bg-emerald-500'
                        ]"
                        :style="{ width: `${Math.max(0, Math.min(100, (((txStatus.audio_level_r ?? -60) + 60) / 60) * 100))}%` }"
                      />
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div v-if="txStatus.running" class="pt-2">
              <button
                @click="stopTx"
                :disabled="txLoading"
                class="w-full py-2.5 px-4 bg-rose-50 dark:bg-rose-950/30 hover:bg-rose-100 dark:hover:bg-rose-900/40 text-rose-700 dark:text-rose-400 border border-rose-200 dark:border-rose-800/60 rounded-xl font-medium text-sm transition"
              >
                Stop Broadcasting
              </button>
            </div>
          </div>
        </div>

        <!-- Encoder Configuration (Right 2 cols) -->
        <div class="lg:col-span-2">
          <div class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-6 space-y-6 shadow-sm">
            <div class="flex items-center justify-between">
              <h2 class="text-base font-bold text-slate-900 dark:text-slate-100 tracking-tight">NDI Encoder Configuration</h2>
              <label class="flex items-center gap-2 cursor-pointer bg-slate-50 dark:bg-slate-950 px-3 py-1.5 rounded-xl border border-slate-200 dark:border-slate-800">
                <input
                  type="checkbox"
                  v-model="txAutoStart"
                  @change="toggleTxAutoStart"
                  class="rounded bg-white dark:bg-slate-900 border-slate-300 dark:border-slate-700 text-[#C7000A] focus:ring-[#C7000A] h-4 w-4"
                />
                <span class="text-xs font-semibold text-slate-700 dark:text-slate-300">Auto-Start on Boot</span>
              </label>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
              <!-- NDI Stream Name -->
              <div>
                <label class="block text-xs font-bold text-slate-700 dark:text-slate-300 mb-1">NDI Stream Name</label>
                <input
                  type="text"
                  v-model="txSenderName"
                  :disabled="txStatus.running"
                  class="w-full bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-900 dark:text-slate-100 focus:outline-none focus:border-[#C7000A]"
                  placeholder="TX"
                />
              </div>

              <!-- Video Capture Device -->
              <div>
                <label class="block text-xs font-bold text-slate-700 dark:text-slate-300 mb-1">Camera / Video Device</label>
                <select
                  v-model="txVideoDevice"
                  :disabled="txStatus.running"
                  class="w-full bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-900 dark:text-slate-100 focus:outline-none focus:border-[#C7000A]"
                >
                  <option v-for="dev in videoDevices" :key="dev.index" :value="dev.index">
                    {{ dev.name }} (index {{ dev.index }})
                  </option>
                </select>
              </div>

              <!-- Target Resolution -->
              <div>
                <label class="block text-xs font-bold text-slate-700 dark:text-slate-300 mb-1">Resolution</label>
                <select
                  v-model="txResolution"
                  :disabled="txStatus.running"
                  class="w-full bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-900 dark:text-slate-100 focus:outline-none focus:border-[#C7000A]"
                >
                  <option value="1920x1080">1920 x 1080 (Full HD)</option>
                  <option value="1280x720">1280 x 720 (HD)</option>
                  <option value="640x480">640 x 480 (SD)</option>
                </select>
              </div>

              <!-- FPS -->
              <div>
                <label class="block text-xs font-bold text-slate-700 dark:text-slate-300 mb-1">Frame Rate (FPS)</label>
                <select
                  v-model="txFps"
                  :disabled="txStatus.running"
                  class="w-full bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-900 dark:text-slate-100 focus:outline-none focus:border-[#C7000A]"
                >
                  <option value="60">60 FPS</option>
                  <option value="59.94">59.94 FPS</option>
                  <option value="30">30 FPS</option>
                  <option value="29.97">29.97 FPS</option>
                  <option value="24">24 FPS</option>
                </select>
              </div>

              <!-- Pixel Format -->
              <div>
                <label class="block text-xs font-bold text-slate-700 dark:text-slate-300 mb-1">Color Format</label>
                <select
                  v-model="txPixFmt"
                  :disabled="txStatus.running"
                  class="w-full bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-900 dark:text-slate-100 focus:outline-none focus:border-[#C7000A]"
                >
                  <option value="BGRX">BGRX (Default)</option>
                  <option value="BGRA">BGRA</option>
                  <option value="RGBA">RGBA</option>
                </select>
              </div>

              <!-- Audio Enable -->
              <div class="flex items-end pb-2">
                <label class="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    v-model="txNoAudio"
                    :disabled="txStatus.running"
                    class="rounded bg-white dark:bg-slate-950 border-slate-300 dark:border-slate-700 text-[#C7000A] focus:ring-[#C7000A] h-4 w-4"
                  />
                  <span class="text-sm font-medium text-slate-700 dark:text-slate-300">Disable Audio (Video only)</span>
                </label>
              </div>

              <!-- Audio Device (if enabled) -->
              <div v-if="!txNoAudio" class="md:col-span-2">
                <label class="block text-xs font-bold text-slate-700 dark:text-slate-300 mb-1">Audio Input Device</label>
                <select
                  v-model="txAudioDevice"
                  :disabled="txStatus.running"
                  class="w-full bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-900 dark:text-slate-100 focus:outline-none focus:border-[#C7000A]"
                >
                  <option v-for="dev in audioDevices" :key="dev.index" :value="dev.index">
                    {{ dev.name }} ({{ dev.max_input_channels }}ch, {{ dev.default_samplerate }}Hz)
                  </option>
                </select>
              </div>
            </div>

            <!-- Start button -->
            <div class="pt-4 border-t border-slate-100 dark:border-slate-800">
              <button
                v-if="!txStatus.running"
                @click="startTx"
                :disabled="txLoading"
                class="w-full py-3 px-6 bg-[#C7000A] hover:bg-[#b00009] text-white rounded-xl font-bold text-sm transition shadow-md shadow-[#C7000A]/20"
              >
                Start NDI Broadcasting
              </button>
              <button
                v-else
                @click="stopTx"
                :disabled="txLoading"
                class="w-full py-3 px-6 bg-rose-600 hover:bg-rose-500 text-white rounded-xl font-bold text-sm transition shadow-md shadow-rose-600/20"
              >
                Stop Broadcasting
              </button>
            </div>

          </div>
        </div>

      </div>


      <!-- ==================== TAB: Multi-View ==================== -->
      <div v-if="activeTab === 'multiview'" class="space-y-6">
        
        <!-- Multi-Viewer Controls Header -->
        <div class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-5 shadow-sm flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div>
            <div class="flex items-center gap-3">
              <h2 class="text-base font-bold text-slate-900 dark:text-slate-100 tracking-tight">NDI Multi-Viewer Grid</h2>
              <span class="text-xs bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 font-semibold px-2.5 py-0.5 rounded-full border border-slate-200 dark:border-slate-700">
                {{ ndiSources.length }} Sources Active
              </span>
            </div>
            <p class="text-xs text-slate-500 dark:text-slate-400 mt-1">
              Live web grid preview with stereo audio level meters and physical SDL2 display output control.
            </p>
          </div>

          <!-- SDL2 Multi-Viewer Window Controls -->
          <div class="flex items-center gap-3">
            <label class="flex items-center gap-2 cursor-pointer text-xs font-medium text-slate-600 dark:text-slate-300">
              <input
                type="checkbox"
                v-model="multiviewFullscreen"
                :disabled="multiviewStatus.running"
                class="rounded bg-white dark:bg-slate-950 border-slate-300 dark:border-slate-700 text-[#C7000A] focus:ring-[#C7000A] h-4 w-4"
              />
              <span>Fullscreen</span>
            </label>

            <button
              v-if="!multiviewStatus.running"
              @click="startMultiview"
              :disabled="multiviewLoading"
              class="py-2 px-4 bg-[#C7000A] hover:bg-[#b00009] text-white rounded-xl text-xs font-bold transition shadow-sm flex items-center gap-2"
            >
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 10h16M4 14h16M4 18h16" />
              </svg>
              <span>Open Display Window</span>
            </button>
            <button
              v-else
              @click="stopMultiview"
              :disabled="multiviewLoading"
              class="py-2 px-4 bg-rose-600 hover:bg-rose-500 text-white rounded-xl text-xs font-bold transition shadow-sm flex items-center gap-2"
            >
              <span>Close Display Window</span>
            </button>
          </div>
        </div>

        <!-- Multi-View Grid (Auto-calculated: 1, 2, 4, 6, 9 cols) -->
        <div
          v-if="ndiSources.length > 0"
          class="grid gap-4"
          :class="[
            ndiSources.length === 1 ? 'grid-cols-1 max-w-4xl mx-auto' :
            ndiSources.length === 2 ? 'grid-cols-1 md:grid-cols-2' :
            ndiSources.length <= 4 ? 'grid-cols-1 sm:grid-cols-2' :
            ndiSources.length <= 6 ? 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3' :
            'grid-cols-1 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-4'
          ]"
        >
          <div
            v-for="source in ndiSources"
            :key="source.name"
            class="bg-black border border-slate-800 rounded-2xl overflow-hidden shadow-lg flex flex-col relative group"
          >
            <!-- Video Container -->
            <div class="relative aspect-video w-full bg-slate-950 flex items-center justify-center overflow-hidden">
              <NdiPreviewPlayer
                :source="source.name"
                :fps="24"
                :width="480"
                :api-base="API_BASE"
              />

              <!-- Top Left: NDI Source Label -->
              <div class="absolute top-2.5 left-2.5 bg-black/80 backdrop-blur-md px-2.5 py-1 rounded-lg border border-white/10 flex items-center gap-2 max-w-[80%]">
                <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
                <span class="text-xs font-mono font-bold text-white truncate">{{ source.name }}</span>
              </div>

              <!-- Top Right: Switch RX shortcut button -->
              <div class="absolute top-2.5 right-2.5 opacity-0 group-hover:opacity-100 transition-opacity">
                <button
                  @click="rxStatus.running ? switchRx(source.name) : startRx(source.name)"
                  class="bg-[#C7000A] hover:bg-[#b00009] text-white text-[11px] font-bold px-2.5 py-1 rounded-lg shadow transition"
                  title="Switch main viewer output to this source"
                >
                  {{ rxStatus.running && rxStatus.current_source === source.name ? 'Active RX' : 'Send to RX' }}
                </button>
              </div>

              <!-- Bottom: Real-time Audio VU Meter Overlay -->
              <div class="absolute bottom-2 inset-x-2.5 bg-black/80 backdrop-blur-md px-2.5 py-1.5 rounded-lg border border-white/10 space-y-1">
                <!-- Channel L -->
                <div class="flex items-center gap-2">
                  <span class="text-[9px] font-mono font-semibold text-slate-400 w-2.5">L</span>
                  <div class="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden flex">
                    <div
                      class="h-full transition-all duration-75 ease-out rounded-full"
                      :class="[
                        rxStatus.current_source === source.name ? (
                          (rxStatus.audio_peak_l ?? -60) > -3 ? 'bg-rose-500' :
                          (rxStatus.audio_peak_l ?? -60) > -12 ? 'bg-amber-400' : 'bg-emerald-500'
                        ) : 'bg-emerald-500/70'
                      ]"
                      :style="{ width: `${rxStatus.current_source === source.name ? Math.max(0, Math.min(100, (((rxStatus.audio_level_l ?? -60) + 60) / 60) * 100)) : 40}%` }"
                    />
                  </div>
                </div>

                <!-- Channel R -->
                <div class="flex items-center gap-2">
                  <span class="text-[9px] font-mono font-semibold text-slate-400 w-2.5">R</span>
                  <div class="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden flex">
                    <div
                      class="h-full transition-all duration-75 ease-out rounded-full"
                      :class="[
                        rxStatus.current_source === source.name ? (
                          (rxStatus.audio_peak_r ?? -60) > -3 ? 'bg-rose-500' :
                          (rxStatus.audio_peak_r ?? -60) > -12 ? 'bg-amber-400' : 'bg-emerald-500'
                        ) : 'bg-emerald-500/70'
                      ]"
                      :style="{ width: `${rxStatus.current_source === source.name ? Math.max(0, Math.min(100, (((rxStatus.audio_level_r ?? -60) + 60) / 60) * 100)) : 40}%` }"
                    />
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Empty State -->
        <div v-else class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-12 text-center space-y-3">
          <div class="w-12 h-12 rounded-2xl bg-slate-100 dark:bg-slate-800 flex items-center justify-center mx-auto text-slate-400">
            <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
            </svg>
          </div>
          <h3 class="text-sm font-bold text-slate-800 dark:text-slate-200">No NDI Sources Detected</h3>
          <p class="text-xs text-slate-500 dark:text-slate-400 max-w-sm mx-auto">
            Discovered NDI feeds on your local network will automatically appear here in an auto-arranged grid layout.
          </p>
        </div>

      </div>

    </main>

    <!-- Web Preview Modal -->
    <div
      v-if="isPreviewModalOpen"
      class="fixed inset-0 z-50 flex items-center justify-center p-4 sm:p-6 bg-slate-950/70 backdrop-blur-sm transition-opacity"
    >
      <div class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl w-full max-w-4xl shadow-2xl overflow-hidden flex flex-col max-h-[90vh]">
        <!-- Modal Header -->
        <div class="px-5 py-3.5 border-b border-slate-100 dark:border-slate-800 flex items-center justify-between bg-slate-50/50 dark:bg-slate-900/50">
          <div class="flex items-center gap-3">
            <div class="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-red-50 dark:bg-red-950/50 border border-red-200 dark:border-red-900 text-[#C7000A] text-xs font-semibold uppercase tracking-wider">
              <span class="w-2 h-2 rounded-full bg-[#C7000A] animate-pulse"></span>
              <span>LIVE PREVIEW ({{ previewMode === 'webrtc' ? 'WebRTC' : 'MJPEG' }})</span>
            </div>
            <h3 class="text-sm font-bold text-slate-900 dark:text-slate-100 truncate max-w-[300px] sm:max-w-md">
              {{ previewSource }}
            </h3>
          </div>
          <button
            @click="closePreview"
            class="p-1.5 rounded-lg text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 transition"
          >
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <!-- Video Stream Container -->
        <div class="relative bg-black flex items-center justify-center aspect-video w-full overflow-hidden select-none">
          <!-- WebRTC Real-time Video Stream -->
          <video
            v-show="previewMode === 'webrtc'"
            ref="webrtcVideoRef"
            autoplay
            playsinline
            muted
            class="w-full h-full object-contain"
          ></video>
          <!-- Fallback MJPEG Image Stream -->
          <img
            v-if="previewMode === 'mjpeg' && previewSource"
            :key="previewKey"
            :src="`${API_BASE}/api/ndi/preview?source=${encodeURIComponent(previewSource)}&fps=15&width=640&t=${previewKey}`"
            alt="NDI Preview"
            class="w-full h-full object-contain pointer-events-none"
            @error="console.warn('Preview stream error or connection closed')"
          />
        </div>

        <!-- Modal Footer Actions -->
        <div class="px-5 py-3.5 border-t border-slate-100 dark:border-slate-800 flex items-center justify-between bg-slate-50/50 dark:bg-slate-900/50">
          <p class="text-xs text-slate-500 dark:text-slate-400">
            {{ previewMode === 'webrtc' ? 'Real-time WebRTC Stream (<100ms ultra-low latency).' : 'MJPEG Fallback Stream.' }} Closing this modal stops streaming to save resources.
          </p>
          <div class="flex items-center gap-2">
            <button
              v-if="previewSource && (!rxStatus.running || rxStatus.current_source !== previewSource)"
              @click="rxStatus.running ? switchRx(previewSource) : startRx(previewSource)"
              class="py-2 px-4 bg-[#C7000A] hover:bg-[#b00009] text-white rounded-xl text-xs font-bold transition shadow-sm"
            >
              {{ rxStatus.running ? 'Switch Output Window' : 'Open Output Window' }}
            </button>
            <button
              @click="closePreview"
              class="py-2 px-4 bg-slate-100 dark:bg-slate-800 hover:bg-slate-200 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-200 rounded-xl text-xs font-medium transition"
            >
              Close
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
