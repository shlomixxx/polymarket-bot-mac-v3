/** Single AudioContext — browsers usually require a gesture before resume(). */
let streamAudioCtx: AudioContext | null = null;

function getStreamAudioContext(): AudioContext | null {
  try {
    const AC = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AC) return null;
    if (!streamAudioCtx || streamAudioCtx.state === "closed") {
      streamAudioCtx = new AC();
    }
    return streamAudioCtx;
  } catch {
    return null;
  }
}

/** Call after click/tap to unlock playback. */
export async function resumeStreamAudio(): Promise<boolean> {
  const ctx = getStreamAudioContext();
  if (!ctx) return false;
  try {
    await ctx.resume();
    return ctx.state === "running";
  } catch {
    return false;
  }
}

export function playEntryChime(): void {
  const ctx = getStreamAudioContext();
  if (!ctx) return;
  const run = () => {
    if (ctx.state !== "running") return;
    try {
      const now = ctx.currentTime;
      const notes: [number, number, number][] = [
        [523.25, 0, 0.14],
        [659.25, 0.11, 0.16],
        [783.99, 0.24, 0.2],
      ];
      for (const [freq, delay, dur] of notes) {
        const o = ctx.createOscillator();
        const g = ctx.createGain();
        o.type = "sine";
        o.frequency.value = freq;
        o.connect(g);
        g.connect(ctx.destination);
        const t0 = now + delay;
        g.gain.setValueAtTime(0.0001, t0);
        g.gain.exponentialRampToValueAtTime(0.2, t0 + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
        o.start(t0);
        o.stop(t0 + dur + 0.02);
      }
    } catch {
      /* ignore */
    }
  };
  void ctx.resume().then(() => {
    run();
  });
}

export function playExitChime(): void {
  const ctx = getStreamAudioContext();
  if (!ctx) return;
  const run = () => {
    if (ctx.state !== "running") return;
    try {
      const now = ctx.currentTime;
      const notes: [number, number, number][] = [
        [783.99, 0, 0.12],
        [659.25, 0.09, 0.13],
        [523.25, 0.2, 0.15],
        [392.0, 0.34, 0.18],
      ];
      for (const [freq, delay, dur] of notes) {
        const o = ctx.createOscillator();
        const g = ctx.createGain();
        o.type = "triangle";
        o.frequency.value = freq;
        o.connect(g);
        g.connect(ctx.destination);
        const t0 = now + delay;
        g.gain.setValueAtTime(0.0001, t0);
        g.gain.exponentialRampToValueAtTime(0.14, t0 + 0.025);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
        o.start(t0);
        o.stop(t0 + dur + 0.02);
      }
    } catch {
      /* ignore */
    }
  };
  void ctx.resume().then(() => {
    run();
  });
}
