/* Live dictation: turns the microphone into 16 kHz mono 16-bit PCM in blocks of 0.1 s, plus a loudness level for the mic
   button. Outputs nothing (the node feeds a muted gain only so the browser keeps it running): no sound ever plays. */
class KchPcm extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;        // 1 when the context already runs at 16 kHz
    this.pos = 0;                            // where the next 16 kHz sample falls in the input, in input samples
    this.buf = new Int16Array(1600);
    this.n = 0;
    this.peak = 0;
    this.port.onmessage = (e) => {             // stopping: send what is left of the block now
      if (e.data !== 'flush') return;
      const rest = this.buf.slice(0, this.n);
      this.n = 0;
      this.port.postMessage({ pcm: rest.buffer, peak: this.peak, flushed: true }, [rest.buffer]);
    };
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) { const a = Math.abs(ch[i]); if (a > this.peak) this.peak = a; }
    if (this.ratio === 1) {
      for (let i = 0; i < ch.length; i++) this.push(ch[i]);
    } else {                                 // another rate: linear interpolation down to 16 kHz
      while (this.pos < ch.length) {
        const i = Math.floor(this.pos), f = this.pos - i;
        const a = ch[i], b = i + 1 < ch.length ? ch[i + 1] : a;
        this.push(a + (b - a) * f);
        this.pos += this.ratio;
      }
      this.pos -= ch.length;
    }
    return true;
  }

  push(x) {
    const v = Math.max(-1, Math.min(1, x));
    this.buf[this.n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
    if (this.n === this.buf.length) {
      this.port.postMessage({ pcm: this.buf.buffer, peak: this.peak }, [this.buf.buffer]);
      this.buf = new Int16Array(1600);
      this.n = 0;
      this.peak = 0;
    }
  }
}
registerProcessor('kch-pcm', KchPcm);
