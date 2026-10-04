<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    source: string
    fps?: number
    width?: number
    mjpegFallback?: boolean
    autoStart?: boolean
    apiBase?: string
  }>(),
  {
    fps: 24,
    width: 360,
    mjpegFallback: true,
    autoStart: true,
    apiBase: ''
  }
)

const videoRef = ref<HTMLVideoElement | null>(null)
const mode = ref<'webrtc' | 'mjpeg'>('webrtc')
const mjpegKey = ref(Date.now())
const isConnected = ref(false)
let pc: RTCPeerConnection | null = null
let destroyed = false

async function startStream() {
  stopStream()
  if (!props.source) return

  mode.value = 'webrtc'
  isConnected.value = false

  try {
    const peer = new RTCPeerConnection({
      iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
    })
    pc = peer

    peer.addTransceiver('video', { direction: 'recvonly' })

    peer.ontrack = (event) => {
      if (destroyed) return
      if (videoRef.value && event.streams && event.streams[0]) {
        videoRef.value.srcObject = event.streams[0]
        isConnected.value = true
      }
    }

    const offer = await peer.createOffer()
    if (destroyed) {
      peer.close()
      return
    }
    await peer.setLocalDescription(offer)

    const res = await fetch(`${props.apiBase}/api/webrtc/offer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source: props.source,
        sdp: peer.localDescription?.sdp,
        type: peer.localDescription?.type
      })
    })

    if (!res.ok) {
      throw new Error(`Signaling failed: ${res.statusText}`)
    }

    const answer = await res.json()
    if (destroyed) {
      peer.close()
      return
    }
    await peer.setRemoteDescription(new RTCSessionDescription(answer))
  } catch (err) {
    if (destroyed) return
    console.warn(`WebRTC stream failed for ${props.source}, falling back to MJPEG:`, err)
    if (props.mjpegFallback) {
      mode.value = 'mjpeg'
      mjpegKey.value = Date.now()
    }
  }
}

function stopStream() {
  if (pc) {
    pc.close()
    pc = null
  }
  if (videoRef.value) {
    videoRef.value.srcObject = null
  }
  isConnected.value = false
}

watch(
  () => props.source,
  (newSrc, oldSrc) => {
    if (newSrc !== oldSrc && props.autoStart) {
      startStream()
    }
  }
)

onMounted(() => {
  if (props.autoStart) {
    startStream()
  }
})

onBeforeUnmount(() => {
  destroyed = true
  stopStream()
})
</script>

<template>
  <div class="relative w-full h-full bg-black flex items-center justify-center overflow-hidden select-none">
    <!-- WebRTC Video Stream (Real-time 24fps) -->
    <video
      v-show="mode === 'webrtc'"
      ref="videoRef"
      autoplay
      playsinline
      muted
      class="w-full h-full object-contain pointer-events-none"
    ></video>

    <!-- MJPEG Fallback Stream -->
    <img
      v-if="mode === 'mjpeg' && source"
      :key="mjpegKey"
      :src="`${apiBase}/api/ndi/preview?source=${encodeURIComponent(source)}&fps=${fps}&width=${width}&t=${mjpegKey}`"
      :alt="source"
      class="w-full h-full object-contain pointer-events-none"
      loading="lazy"
    />
  </div>
</template>
