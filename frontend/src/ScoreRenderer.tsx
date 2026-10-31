import { useEffect, useRef, useState } from "react";
import { LoaderCircle } from "lucide-react";
import type {
  OpenSheetMusicDisplay,
  GraphicalNote,
  VexFlowGraphicalNote,
} from "opensheetmusicdisplay";
import type { CanonicalScore } from "./api";

type Binding = {
  glyph: VexFlowGraphicalNote;
  ids: string[];
  elements: HTMLElement[];
  paintState?: string;
};
const beat = (value: { numerator: number; denominator: number }) =>
  value.numerator / value.denominator;

export default function ScoreRenderer({
  musicxml,
  zoom,
  score,
  selectedIds = [],
  activeIds = [],
  onSelect,
}: {
  musicxml: string;
  zoom: number;
  score: CanonicalScore | null;
  selectedIds?: string[];
  activeIds?: string[];
  onSelect?: (ids: string[]) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const renderer = useRef<OpenSheetMusicDisplay | null>(null);
  const bindings = useRef<Binding[]>([]);
  const latest = useRef({ zoom, selectedIds, activeIds, onSelect });
  latest.current = { zoom, selectedIds, activeIds, onSelect };
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState("");
  const paint = () => {
    const selected = new Set(latest.current.selectedIds),
      active = new Set(latest.current.activeIds);
    for (const binding of bindings.current) {
      const selectedNote = binding.ids.some((id) => selected.has(id));
      const activeNote = binding.ids.some((id) => active.has(id));
      const paintState = `${selectedNote}:${activeNote}`;
      if (binding.paintState === paintState) continue;
      binding.paintState = paintState;
      binding.glyph.setColor(
        selectedNote ? "#b45a13" : activeNote ? "#267d67" : "#172c25",
        { applyToNoteheads: true, applyToStem: true, applyToLedgerLines: true },
      );
      for (const element of binding.elements) {
        element.classList.toggle("score-note-selected", selectedNote);
        element.classList.toggle("score-note-playing", activeNote);
        element.setAttribute("aria-pressed", String(selectedNote));
      }
    }
    container.current
      ?.querySelectorAll(".score-playhead")
      .forEach((el) => el.remove());
    const playing = bindings.current.find((b) =>
      b.ids.some((id) => active.has(id)),
    );
    const head = playing?.elements[0];
    const svg = head?.closest("svg");
    if (head && svg) {
      const box = head.getBoundingClientRect(),
        matrix = svg.getScreenCTM();
      if (matrix) {
        const point = new DOMPoint(box.left - 4, box.top - 16).matrixTransform(
          matrix.inverse(),
        );
        const cursor = document.createElementNS(
          "http://www.w3.org/2000/svg",
          "rect",
        );
        cursor.setAttribute("class", "score-playhead");
        cursor.setAttribute("x", String(point.x));
        cursor.setAttribute("y", String(point.y));
        cursor.setAttribute("width", "2.5");
        cursor.setAttribute("height", "55");
        cursor.setAttribute("fill", "#267d67");
        cursor.setAttribute("opacity", "0.55");
        cursor.setAttribute("pointer-events", "none");
        svg.append(cursor);
      }
    }
  };
  const paintRef = useRef(paint);
  paintRef.current = paint;
  useEffect(() => {
    paintRef.current();
  }, [selectedIds, activeIds]);
  const rebind = useRef<() => void>(() => {});
  useEffect(() => {
    if (renderer.current?.IsReadyToRender()) {
      try {
        renderer.current.Zoom = zoom;
        renderer.current.render();
        rebind.current();
      } catch {
        setError("The score could not be zoomed. Regenerate the preview.");
        setState("error");
      }
    }
  }, [zoom]);
  useEffect(() => {
    const host = container.current;
    if (!host) return;
    let disposed = false,
      instance: OpenSheetMusicDisplay | null = null,
      observer: ResizeObserver | null = null,
      timer: ReturnType<typeof setTimeout> | undefined;
    const surface = document.createElement("div");
    host.append(surface);
    setState("loading");
    setError("");
    const bind = () => {
      bindings.current = [];
      if (!score || !instance) return;
      const staves = score.parts.flatMap((part) => part.staves);
      const source = new Map(score.source_notes.map((note) => [note.id, note]));
      // OSMD does not retain MusicXML note IDs. Associate its parsed staff,
      // measure, voice, beat and pitch with canonical events, then attach the
      // preserved source IDs to the notehead elements. Ambiguous matches stay
      // unbound; the renderer is never treated as the editable data store.
      instance.GraphicSheet.MeasureList.forEach(
        (staffMeasures, measureIndex) => {
          staffMeasures.forEach((measure, staffIndex) => {
            const canonical = staves[staffIndex]?.measures[measureIndex];
            if (!canonical) return;
            for (const entry of measure.staffEntries)
              for (const voice of entry.graphicalVoiceEntries)
                for (const g of voice.notes) {
                  const note = g.sourceNote;
                  if (note.isRest()) continue;
                  // OSMD halfTone numbers C0 as 0; MIDI numbers C-1 as 0.
                  const midiPitch = note.halfTone + 12;
                  const offset = note.ParentVoiceEntry.Timestamp.RealValue * 4;
                  const voiceId = String(
                    note.ParentVoiceEntry.ParentVoice.VoiceId,
                  );
                  const possible = canonical.voices
                    .flatMap((v) =>
                      v.events.map((event) => ({ event, voice: v.id })),
                    )
                    .filter(
                      ({ event }) =>
                        event.kind !== "rest" &&
                        Math.abs(beat(event.offset) - offset) < 0.00001 &&
                        event.pitches.includes(midiPitch),
                    );
                  const exact = possible.filter(
                    (item) => item.voice === voiceId,
                  );
                  const match =
                    exact.length === 1
                      ? exact[0]
                      : possible.length === 1
                        ? possible[0]
                        : null;
                  if (!match) continue;
                  const ids = match.event.source_note_ids.filter(
                    (id) =>
                      source.get(id)?.pitch ===
                      midiPitch - score.settings.transpose_semitones,
                  );
                  if (!ids.length) continue;
                  const glyph = g as GraphicalNote & VexFlowGraphicalNote;
                  const heads = glyph.getNoteheadSVGs?.() ?? [];
                  const element =
                    heads[glyph.vfnoteIndex] ??
                    heads[0] ??
                    glyph.getSVGGElement?.();
                  const elements = element
                    ? [element as unknown as HTMLElement]
                    : [];
                  for (const el of elements) {
                    el.dataset.sourceNoteIds = ids.join(" ");
                    el.setAttribute("role", "button");
                    el.setAttribute("tabindex", "0");
                    el.setAttribute(
                      "aria-label",
                      `Select score note MIDI ${midiPitch}`,
                    );
                    el.style.cursor = "pointer";
                    el.onclick = (event) => {
                      event.stopPropagation();
                      latest.current.onSelect?.(ids);
                    };
                    el.onkeydown = (event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        latest.current.onSelect?.(ids);
                      }
                    };
                  }
                  bindings.current.push({ glyph, ids, elements });
                }
          });
        },
      );
      paintRef.current();
    };
    rebind.current = bind;
    void (async () => {
      try {
        const { OpenSheetMusicDisplay } = await import("opensheetmusicdisplay");
        if (disposed) return;
        instance = new OpenSheetMusicDisplay(surface, {
          autoResize: false,
          backend: "svg",
          // OSMD's single-line SVG title can extend past both page edges.
          // Render the canonical title as wrapping HTML above the score.
          drawTitle: !score?.title,
          drawComposer: false,
          drawPartNames: true,
        });
        instance.setLogLevel("warn");
        await instance.load(musicxml);
        if (disposed) return;
        instance.Zoom = latest.current.zoom;
        instance.render();
        renderer.current = instance;
        bind();
        setState("ready");
        let width = host.clientWidth;
        observer = new ResizeObserver(() => {
          if (host.clientWidth === width) return;
          width = host.clientWidth;
          clearTimeout(timer);
          timer = setTimeout(() => {
            if (disposed || !instance?.IsReadyToRender()) return;
            try {
              instance.render();
              bind();
            } catch {
              setError(
                "The score could not be resized. Regenerate the preview.",
              );
              setState("error");
            }
          }, 150);
        });
        observer.observe(host);
      } catch (e) {
        if (!disposed) {
          setState("error");
          setError(
            `The score renderer could not display this MusicXML. ${(e as Error).message}`,
          );
        }
      }
    })();
    return () => {
      disposed = true;
      clearTimeout(timer);
      observer?.disconnect();
      if (renderer.current === instance) renderer.current = null;
      bindings.current = [];
      instance?.clear();
      surface.remove();
    };
  }, [musicxml, score]);
  return (
    <>
      {state === "loading" && (
        <p className="score-render-status" role="status">
          <LoaderCircle size={16} className="spin" />
          Engraving the score…
        </p>
      )}
      {state === "error" && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}
      <div
        className="score-paper-scroll"
        tabIndex={0}
        role="region"
        aria-label="Rendered sheet music; scroll horizontally if needed"
      >
        {score?.title && <h3 className="score-paper-title">{score.title}</h3>}
        <div
          className="score-paper"
          ref={container}
          data-testid="score-renderer"
          aria-busy={state === "loading"}
        />
      </div>
    </>
  );
}
