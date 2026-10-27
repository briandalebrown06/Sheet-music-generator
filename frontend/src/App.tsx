import { useEffect, useRef, useState } from 'react'
import './App.css'

type Status = 'queued' | 'processing' | 'completed' | 'failed'
type Job = { id: string; status: Status; error: string | null; created_at: string }
type Note = { start: number; end: number; pitch: number; amplitude: number }
type Project = {
  id: string; title: string; filename: string; created_at: string; status?: Status
  jobs?: Job[]; notes?: Note[] | null
}
type Health = { ffmpeg_available: boolean; engine: string }

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, options)
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(typeof body?.detail === 'string' ? body.detail : `Request failed (${response.status}).`)
  }
  return response.json()
}

function selectedProject() {
  const match = window.location.hash.match(/^#\/projects\/([a-f0-9]{32})$/)
  return match?.[1] ?? null
}

function pitchName(pitch: number) {
  return `${['C', 'C♯', 'D', 'D♯', 'E', 'F', 'F♯', 'G', 'G♯', 'A', 'A♯', 'B'][pitch % 12]}${Math.floor(pitch / 12) - 1}`
}

export default function App() {
  const [projectId, setProjectId] = useState(selectedProject)
  const [projects, setProjects] = useState<Project[]>([])
  const [project, setProject] = useState<Project | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState('')
  const [connectionError, setConnectionError] = useState('')
  const [busy, setBusy] = useState(false)
  const [recording, setRecording] = useState(false)
  const [recorded, setRecorded] = useState<{ file: File; url: string } | null>(null)
  const recorder = useRef<MediaRecorder | null>(null)
  const stream = useRef<MediaStream | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mounted = useRef(false)
  const captureRequest = useRef(0)
  const [micPending, setMicPending] = useState(false)

  useEffect(() => {
    mounted.current = true
    const change = () => {
      captureRequest.current += 1
      if (recorder.current?.state === 'recording') recorder.current.stop()
      stream.current?.getTracks().forEach(track => track.stop())
      setProjectId(selectedProject()); setProject(null); setError('')
    }
    window.addEventListener('hashchange', change)
    return () => {
      mounted.current = false
      captureRequest.current += 1
      window.removeEventListener('hashchange', change)
      if (timer.current) clearTimeout(timer.current)
      if (recorder.current?.state === 'recording') recorder.current.stop()
      stream.current?.getTracks().forEach(track => track.stop())
    }
  }, [])

  useEffect(() => {
    let active = true
    const refresh = async () => {
      try {
        const [items, service, detail] = await Promise.all([
          api<Project[]>('/projects'), api<Health>('/health'),
          projectId ? api<Project>(`/projects/${projectId}`) : Promise.resolve(null),
        ])
        if (active) {
          setProjects(items); setHealth(service); setProject(detail); setConnectionError('')
        }
      } catch (reason) {
        if (active) setConnectionError(reason instanceof Error ? reason.message : 'Cannot reach the local API.')
      }
    }
    void refresh()
    const interval = setInterval(() => void refresh(), 3000)
    return () => { active = false; clearInterval(interval) }
  }, [projectId])

  useEffect(() => {
    return () => { if (recorded) URL.revokeObjectURL(recorded.url) }
  }, [recorded])

  async function upload(file: File) {
    setBusy(true); setError('')
    try {
      const body = new FormData()
      body.append('audio', file)
      const created = await api<Project>('/projects', { method: 'POST', body })
      setRecorded(null)
      window.location.hash = `/projects/${created.id}`
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Upload failed.')
    } finally { setBusy(false) }
  }

  async function startRecording() {
    const request = ++captureRequest.current
    setError(''); setMicPending(true)
    try {
      if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
        throw new Error('Microphone recording is not supported here. Use a current browser on localhost, or upload a file.')
      }
      const media = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (!mounted.current || request !== captureRequest.current) {
        media.getTracks().forEach(track => track.stop()); return
      }
      stream.current = media
      const mimeType = ['audio/webm', 'audio/mp4', 'audio/ogg'].find(type => MediaRecorder.isTypeSupported(type))
      if (!mimeType) throw new Error('This browser has no supported recording format. Please upload a file.')
      const capture = new MediaRecorder(media, { mimeType })
      recorder.current = capture
      const chunks: BlobPart[] = []
      capture.ondataavailable = event => { if (event.data.size) chunks.push(event.data) }
      capture.onstop = () => {
        media.getTracks().forEach(track => track.stop())
        if (timer.current) clearTimeout(timer.current)
        if (mounted.current) {
          const extension = mimeType.includes('mp4') ? 'm4a' : mimeType.includes('ogg') ? 'ogg' : 'webm'
          const file = new File(chunks, `recording.${extension}`, { type: mimeType })
          setRecorded({ file, url: URL.createObjectURL(file) })
          setRecording(false)
        }
      }
      capture.onerror = () => {
        media.getTracks().forEach(track => track.stop())
        if (capture.state !== 'inactive') capture.stop()
        setRecording(false); setError('Recording failed. Please try again or upload a file.')
      }
      setRecorded(null)
      capture.start(1000)
      setRecording(true)
      timer.current = setTimeout(() => { if (capture.state === 'recording') capture.stop() }, 60_000)
    } catch (reason) {
      stream.current?.getTracks().forEach(track => track.stop())
      setError(reason instanceof Error ? reason.message : 'Microphone access was denied.')
    } finally { if (mounted.current) setMicPending(false) }
  }

  async function retry() {
    setBusy(true)
    try {
      setProject(await api<Project>(`/projects/${projectId}/retry`, { method: 'POST' }))
      setError('')
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Retry failed.') }
    finally { setBusy(false) }
  }

  const latest = project?.jobs?.[0]
  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#/"><span aria-hidden="true">♬</span> Score Studio</a>
        <span className="local-badge">Local workspace · single user</span>
      </header>
      <main>
        <nav className="breadcrumbs" aria-label="Breadcrumb"><a href="#/">Workspace</a>{projectId && <span> / Project</span>}</nav>
        {(error || connectionError) && <div role="alert" className="alert">{error || connectionError}</div>}
        {!projectId ? <>
          <section className="intro">
            <p className="eyebrow">From sound to a starting score</p>
            <h1>Capture a melody.<br /><span>Make it your own.</span></h1>
            <p>Start with one voice or one instrument. Keep your audio on this computer and turn a clear solo recording into notes you can review.</p>
          </section>
          <section className="capture-grid" aria-label="Create a project">
            <article className="card">
              <span className="card-icon" aria-hidden="true">↑</span><h2>Upload audio</h2>
              <p>A clean, dry solo recording gives the best starting point.</p>
              <label className={`upload-zone ${busy || recording || micPending ? 'disabled' : ''}`}>
                <strong>{busy ? 'Uploading…' : 'Choose an audio file'}</strong>
                <span>WAV, MP3, FLAC, OGG, WebM, M4A, MP4</span>
                <input type="file" accept=".wav,.mp3,.flac,.ogg,.webm,.m4a,.mp4" disabled={busy || recording || micPending}
                  onChange={event => {
                    const file = event.target.files?.[0]
                    if (file) void upload(file)
                    event.target.value = ''
                  }} />
              </label>
              <small>Default limits: 50 MB · 5 minutes. Configurable on the API.</small>
            </article>
            <article className="card">
              <span className="card-icon" aria-hidden="true">●</span><h2>Record a phrase</h2>
              <p>Allow microphone access, play a short phrase, then listen before uploading.</p>
              <button className={recording ? 'danger' : 'primary'} disabled={busy || micPending}
                onClick={() => recording ? recorder.current?.stop() : void startRecording()}>
                {micPending ? 'Waiting for microphone…' : recording ? 'Stop recording' : 'Start recording'}
              </button>
              <p className="record-status" role="status">{recording ? '● Recording — stops automatically at 60 seconds' : 'Up to 60 seconds · microphone permission required'}</p>
              {recorded && <div className="record-preview">
                <audio controls src={recorded.url} aria-label="Preview microphone recording" />
                <button className="primary" disabled={busy || recording} onClick={() => void upload(recorded.file)}>Use this recording</button>
                <button disabled={busy} onClick={() => setRecorded(null)}>Discard</button>
              </div>}
            </article>
          </section>
          <section className="projects-section">
            <div className="section-heading"><h2>Your projects</h2><span>{projects.length} projects</span></div>
            {projects.length === 0 ? <div className="empty-state">Your workspace is ready. Upload or record your first solo performance.</div> :
              <ul className="project-list">{projects.map(item => <li key={item.id}>
                <a href={`#/projects/${item.id}`}><span><strong>{item.title}</strong><small>{new Date(item.created_at).toLocaleString()}</small></span>
                  <span className={`status ${item.status}`}>{item.status}</span><span aria-hidden="true">→</span>
                </a>
              </li>)}</ul>}
          </section>
        </> : project ? <>
          <section className="detail-heading"><div><p className="eyebrow">Project</p><h1>{project.title}</h1>
            <p>{project.filename} · {new Date(project.created_at).toLocaleString()}</p></div>
            <span className={`status ${latest?.status}`}>{latest?.status}</span>
          </section>
          <section className="card">
            <h2>Original performance</h2>
            <audio controls src={`/api/projects/${project.id}/audio`} aria-label="Original uploaded performance" />
            <p>Your original audio stays local. Only upload audio you have permission to use.</p>
          </section>
          <section className="card processing" aria-live="polite">
            <h2>Transcription</h2>
            {latest?.status === 'queued' && <p>Queued for analysis. Start the separate Python worker if it is not running.</p>}
            {latest?.status === 'processing' && <p>Decoding and analyzing the audio. The first model load can take a few minutes.</p>}
            {latest?.status === 'failed' && <><p className="failure">{latest.error}</p><button disabled={busy} onClick={() => void retry()}>Retry analysis</button></>}
            {latest?.status === 'completed' && <>
              <p>{project.notes?.length ?? 0} detected notes. Machine estimates need musical review; timing, pitch, and overlapping notes may be incorrect.</p>
              {project.notes?.length ? <div className="table-scroll"><table>
                <caption>Raw detected notes — not yet quantized notation</caption>
                <thead><tr><th>Pitch</th><th>Start (s)</th><th>End (s)</th><th>Model amplitude</th></tr></thead>
                <tbody>{project.notes.map((note, index) => <tr key={index}><td>{pitchName(note.pitch)}</td><td>{note.start.toFixed(2)}</td><td>{note.end.toFixed(2)}</td><td>{note.amplitude.toFixed(2)}</td></tr>)}</tbody>
              </table></div> : <p>No notes were detected. Try a clearer solo performance.</p>}
            </>}
          </section>
          <section className="card upcoming"><h2>Score workspace · next stage</h2>
            <p>Editable notation and synthesized playback come next, followed by PDF, MusicXML, and MIDI downloads. These features are not available in this foundation.</p>
            <p>Full-song separation and selected-part transcription are later work, not an automatic orchestral-score promise.</p>
          </section>
          <details className="job-history"><summary>Processing history ({project.jobs?.length})</summary>
            <ul>{project.jobs?.map(job => <li key={job.id}>{new Date(job.created_at).toLocaleString()} — {job.status}{job.error && `: ${job.error}`}</li>)}</ul>
          </details>
        </> : <p role="status">{connectionError ? 'Project could not be loaded.' : 'Loading project…'}</p>}
      </main>
      <footer><span>Private by design. Local-only foundation.</span>
        <span>{health ? `API connected · ${health.ffmpeg_available ? 'FFmpeg ready' : 'FFmpeg missing'} · ${health.engine}` : 'Connecting to local API…'}</span>
      </footer>
    </div>
  )
}
