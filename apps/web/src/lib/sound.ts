// New in-app notification chime: a short two-note tone made with the Web Audio API
// (no sound file, no dependency). Per-browser on/off in localStorage, on by default.
// Browsers only allow audio after the user has interacted with the page; until then
// playChime() is a silent no-op, never an error.

export const SOUND_KEY = "he-notification-sound";

/** On unless this browser turned it off ("off"). */
export function parseSound(stored: string | null): boolean {
  return stored !== "off";
}

/** Chime only when the in-app notification count goes up (not on first load, not on drops). */
export function shouldChime(prev: number | null, next: number): boolean {
  return prev !== null && next > prev;
}

export function soundEnabled(): boolean {
  try {
    return parseSound(localStorage.getItem(SOUND_KEY));
  } catch {
    return true;
  }
}

export function setSoundEnabled(on: boolean): void {
  try {
    localStorage.setItem(SOUND_KEY, on ? "on" : "off");
  } catch {
    // private mode: the choice just won't persist
  }
}

let ctx: AudioContext | null = null;

/** Two soft sine notes (E5 then A5), ~0.35 s, low volume. */
export function playChime(): void {
  try {
    ctx ??= new AudioContext();
    if (ctx.state === "suspended") void ctx.resume();
    const t0 = ctx.currentTime;
    [659.25, 880].forEach((freq, i) => {
      const osc = ctx!.createOscillator();
      const gain = ctx!.createGain();
      const start = t0 + i * 0.12;
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.15, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.22);
      osc.connect(gain).connect(ctx!.destination);
      osc.start(start);
      osc.stop(start + 0.25);
    });
  } catch {
    // no Web Audio (old browser / blocked): stay silent
  }
}
