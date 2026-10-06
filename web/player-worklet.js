// Gapless PCM player. Receives Float32 chunks tagged with a sentence seq and plays them
// back to back. Reports when each seq starts playing (to know what the user heard) and
// when the queue drains.
class PcmPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = []; // [{seq, data: Float32Array}]
    this.offset = 0; // read position in queue[0]
    this.paused = false;
    this.active = false; // has audio queued or playing
    this.lastSeq = -1;
    this.port.onmessage = (e) => {
      const m = e.data;
      if (m.type === "push") {
        this.queue.push({ seq: m.seq, data: m.pcm });
        if (!this.active) {
          this.active = true;
          this.port.postMessage({ type: "active" });
        }
      } else if (m.type === "flush") {
        this.queue = [];
        this.offset = 0;
        this.lastSeq = -1;
        this.paused = false;
        this.setIdle();
      } else if (m.type === "pause") {
        this.paused = true;
      } else if (m.type === "resume") {
        this.paused = false;
      }
    };
  }

  setIdle() {
    if (this.active) {
      this.active = false;
      this.port.postMessage({ type: "idle" });
    }
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (this.paused || this.queue.length === 0) {
      out.fill(0);
      if (!this.paused && this.queue.length === 0) this.setIdle();
      return true;
    }
    let i = 0;
    while (i < out.length && this.queue.length) {
      const head = this.queue[0];
      if (this.offset === 0 && head.seq !== this.lastSeq) {
        this.lastSeq = head.seq;
        this.port.postMessage({ type: "started", seq: head.seq });
      }
      const n = Math.min(out.length - i, head.data.length - this.offset);
      out.set(head.data.subarray(this.offset, this.offset + n), i);
      i += n;
      this.offset += n;
      if (this.offset >= head.data.length) {
        this.queue.shift();
        this.offset = 0;
      }
    }
    if (i < out.length) out.fill(0, i);
    return true;
  }
}

registerProcessor("pcm-player", PcmPlayer);
