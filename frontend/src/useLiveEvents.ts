import { useEffect, useRef, useState } from 'react'
import type { SwarmEvent } from './types'

const websocketUrl = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`

/** Subscribes to the backend event relay and reconnects with backoff when the socket drops. */
export function useLiveEvents(onEvent: (event: SwarmEvent) => void, onReconnect: () => void) {
  const [connected, setConnected] = useState(false)
  const handlers = useRef({ onEvent, onReconnect })
  handlers.current = { onEvent, onReconnect }

  useEffect(() => {
    let socket: WebSocket | null = null
    let retryTimer: number | undefined
    let attempt = 0
    let disposed = false

    const connect = () => {
      socket = new WebSocket(websocketUrl)
      socket.onopen = () => {
        if (attempt > 0) handlers.current.onReconnect()
        attempt = 0
        setConnected(true)
      }
      socket.onmessage = (message) => {
        try { handlers.current.onEvent(JSON.parse(message.data) as SwarmEvent) } catch { /* ignore malformed frames */ }
      }
      socket.onclose = () => {
        setConnected(false)
        if (disposed) return
        attempt += 1
        retryTimer = window.setTimeout(connect, Math.min(10_000, 500 * 2 ** attempt))
      }
      socket.onerror = () => socket?.close()
    }

    connect()
    return () => {
      disposed = true
      window.clearTimeout(retryTimer)
      socket?.close()
    }
  }, [])

  return connected
}
