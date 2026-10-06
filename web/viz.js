// Voice orb: a calm sphere of soft light that gently swells with the voice.
// The caller provides `getLevels()` -> {level: 0..1} each frame.

const VERT = `
attribute vec2 aPos;
void main() { gl_Position = vec4(aPos, 0.0, 1.0); }
`;

const FRAG = `
precision mediump float;
uniform vec2 uRes;
uniform float uTime;
uniform float uLevel;  // smoothed loudness, 0..1
uniform float uScale;  // radius multiplier (already smoothed in JS)
uniform vec3 uA;       // base color
uniform vec3 uB;       // light color

// Cheap smooth value noise: only used at very low frequency for a slow drift.
float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float noise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i), hash(i + vec2(1, 0)), u.x), mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), u.x), u.y);
}

void main() {
  vec2 uv = (gl_FragCoord.xy - 0.5 * uRes) / min(uRes.x, uRes.y);
  float d = length(uv);
  float r = 0.30 * uScale;
  float px = 1.5 / min(uRes.x, uRes.y);
  float inside = 1.0 - smoothstep(r - px, r + px, d);

  vec2 q = uv / r;
  float t = uTime * 0.12;
  // Light comes from a slowly orbiting direction, plus a very soft drifting blob.
  vec2 dir = vec2(cos(t * 2.0), sin(t * 2.0));
  float g = 0.5 + 0.35 * dot(q, dir) * -1.0 + 0.25 * q.y;
  g += 0.35 * (noise(q * 1.2 + vec2(t * 2.0, -t * 1.5)) - 0.5);
  g = clamp(g + 0.15 * uLevel, 0.0, 1.0);
  vec3 col = mix(uA, uB, smoothstep(0.1, 0.95, g));

  // Gentle sphere shading so it reads as a ball, not a flat disc.
  float z = sqrt(max(0.0, 1.0 - dot(q, q)));
  col *= 0.82 + 0.18 * z;
  col += uB * pow(1.0 - z, 3.0) * 0.18;

  float glow = exp(-max(d - r, 0.0) * 20.0) * (0.10 + 0.20 * uLevel) * (1.0 - inside);
  glow *= 1.0 - smoothstep(0.38, 0.5, d);

  gl_FragColor = vec4(col * inside + uB * glow, inside + glow);
}
`;

// Two colors per state: base and light. Kept close together: calm, not flashy.
const STATES = {
  idle:            [[0.30, 0.46, 0.84], [0.76, 0.85, 1.00]],
  listening:       [[0.16, 0.42, 0.95], [0.70, 0.88, 1.00]],
  "user-speaking": [[0.16, 0.45, 0.98], [0.76, 0.91, 1.00]],
  thinking:        [[0.36, 0.38, 0.92], [0.78, 0.78, 1.00]],
  speaking:        [[0.12, 0.45, 1.00], [0.80, 0.93, 1.00]],
  muted:           [[0.38, 0.40, 0.46], [0.70, 0.72, 0.78]],
};

export class VoiceOrb {
  constructor(canvas, getLevels) {
    this.canvas = canvas;
    this.getLevels = getLevels;
    this.state = "idle";
    this.colors = structuredClone(STATES.idle);
    this.level = 0; // smoothed loudness
    this.scale = 1; // spring position
    this.vel = 0; // spring velocity
    this.last = performance.now();
    this.t0 = this.last;
    this.gl = canvas.getContext("webgl", { premultipliedAlpha: true, alpha: true, antialias: false });
    if (this.gl) this.#initGl();
    else this.ctx2d = canvas.getContext("2d");
    new ResizeObserver(() => this.#resize()).observe(canvas);
    this.#resize();
    const loop = (now) => {
      this.#frame(now);
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }

  setState(state) {
    if (STATES[state]) this.state = state;
  }

  #initGl() {
    const gl = this.gl;
    const sh = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const prog = gl.createProgram();
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(prog);
    gl.useProgram(prog);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(prog, "aPos");
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    this.u = {};
    for (const n of ["uRes", "uTime", "uLevel", "uScale", "uA", "uB"]) this.u[n] = gl.getUniformLocation(prog, n);
  }

  #resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    const { width, height } = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.max(1, Math.round(width * dpr));
    this.canvas.height = Math.max(1, Math.round(height * dpr));
    this.gl?.viewport(0, 0, this.canvas.width, this.canvas.height);
  }

  #frame(now) {
    // Time-based smoothing: identical feel at 60 or 120 Hz, no jumps after a dropped frame.
    const dt = Math.min(0.05, (now - this.last) / 1000);
    this.last = now;
    const approach = (cur, target, tau) => cur + (target - cur) * (1 - Math.exp(-dt / tau));

    // Colors drift to the new state over ~0.6 s.
    const target = STATES[this.state];
    for (let i = 0; i < 2; i++) for (let j = 0; j < 3; j++) this.colors[i][j] = approach(this.colors[i][j], target[i][j], 0.25);

    // Loudness: rise in ~90 ms, fall in ~250 ms.
    const raw = Math.min(1, Math.max(0, this.getLevels().level || 0));
    this.level = approach(this.level, raw, raw > this.level ? 0.09 : 0.25);

    // Size follows loudness through a critically damped spring: soft, never jittery.
    const breath = this.state === "idle" || this.state === "thinking" || this.state === "muted"
      ? 0.015 * Math.sin((now - this.t0) / 1000 * (this.state === "thinking" ? 2.4 : 1.2))
      : 0;
    const goal = 1 + 0.12 * this.level + breath;
    const k = 90; // stiffness
    const c = 2 * Math.sqrt(k); // critical damping
    this.vel += (k * (goal - this.scale) - c * this.vel) * dt;
    this.scale += this.vel * dt;

    const time = (now - this.t0) / 1000;
    if (this.gl) {
      const gl = this.gl, u = this.u;
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.uniform2f(u.uRes, this.canvas.width, this.canvas.height);
      gl.uniform1f(u.uTime, time);
      gl.uniform1f(u.uLevel, this.level);
      gl.uniform1f(u.uScale, this.scale);
      gl.uniform3f(u.uA, ...this.colors[0]);
      gl.uniform3f(u.uB, ...this.colors[1]);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    } else {
      this.#draw2d();
    }
  }

  // Fallback without WebGL: a gradient disc.
  #draw2d() {
    const c = this.ctx2d;
    const { width: w, height: h } = this.canvas;
    const r = Math.min(w, h) * 0.3 * this.scale;
    const rgb = (v) => `rgb(${v.map((x) => Math.round(x * 255)).join(",")})`;
    c.clearRect(0, 0, w, h);
    const g = c.createRadialGradient(w / 2 - r * 0.3, h / 2 - r * 0.4, r * 0.1, w / 2, h / 2, r);
    g.addColorStop(0, rgb(this.colors[1]));
    g.addColorStop(1, rgb(this.colors[0]));
    c.fillStyle = g;
    c.beginPath();
    c.arc(w / 2, h / 2, r, 0, Math.PI * 2);
    c.fill();
  }
}
