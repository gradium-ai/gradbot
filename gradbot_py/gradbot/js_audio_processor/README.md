# Browser Audio Processor

Standalone JavaScript library for real-time voice communication in the browser.

## Features

- Microphone capture with echo cancellation, AGC, and noise suppression
- Opus encoding of microphone input (24kHz, mono, optimized for voice)
- Opus decoding of incoming audio
- Jitter-buffered playback via AudioWorklet
- Audio visualization support (input/output analyzers)

## Files

| File | Description |
|------|-------------|
| `audio-processor.js` | Main AudioProcessor class - ties everything together |
| `opus-encoder.js` | OpusEncoder class - encodes PCM to Opus using worker |
| `audio-output-worklet.js` | AudioWorklet for jitter-buffered playback |
| `encoderWorker.min.js` | Web Worker for Opus encoding (from opus-recorder) |
| `decoderWorker.min.js` | Web Worker for Opus decoding (from opus-recorder) |

## Usage

### 1. Include the scripts in your HTML

```html
<!-- Order matters: dependencies first -->
<script src="/js/opus-encoder.js"></script>
<script src="/js/audio-processor.js"></script>
```

Note: `audio-output-worklet.js`, `encoderWorker.min.js`, and `decoderWorker.min.js`
are loaded dynamically by AudioProcessor - they just need to be in the same directory.

### 2. Create and start the processor

```javascript
const processor = new AudioProcessor({
  // Called when encoded Opus data is ready to send
  onEncodedAudio: (opusData) => {
    websocket.send(opusData);
  },

  // Optional: called with playback metrics
  onMetrics: (metrics) => {
    console.log('Buffer:', metrics.bufferMs, 'ms');
  },

  // Path to the JS files (default: '/js')
  basePath: '/static/js',

  // Target sample rate (default: 24000)
  sampleRate: 24000,
});

// Request mic permission and start
await processor.start();
```

### 3. Play incoming audio

```javascript
websocket.onmessage = (event) => {
  if (event.data instanceof ArrayBuffer) {
    processor.playOpusData(new Uint8Array(event.data));
  }
};
```

### 4. Handle interruptions

```javascript
// Reset playback buffer (e.g., when user interrupts)
processor.resetPlayback();
```

### 5. Audio visualization

```javascript
function visualize() {
  const inputLevels = processor.getInputLevels();   // Mic input
  const outputLevels = processor.getOutputLevels(); // Speaker output

  // inputLevels and outputLevels are Uint8Array of frequency data (0-255)
  // Use for drawing visualizers, level meters, etc.

  requestAnimationFrame(visualize);
}
visualize();
```

### 6. Stop when done

```javascript
processor.stop();
```

## Configuration

### AudioProcessor options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `onEncodedAudio` | Function | required | Callback for encoded Opus data |
| `onMetrics` | Function | `() => {}` | Callback for playback metrics |
| `basePath` | string | `'/js'` | Path prefix for worker files |
| `sampleRate` | number | `24000` | Target sample rate |

### Opus encoding settings

The encoder is configured for voice:
- Sample rate: 24kHz
- Frame size: 20ms
- Frames per page: 2 (40ms chunks)
- Channels: 1 (mono)
- Application: VOIP (2049)

### Playback buffer settings

The AudioWorklet automatically adapts its buffer:
- Initial buffer: ~80ms before playback starts
- Increases buffer on underruns
- Drops packets if buffer exceeds max
- Crossfade on start/stop to avoid clicks

## Browser Support

Requires:
- Web Audio API (AudioContext, AudioWorklet)
- MediaDevices API (getUserMedia)
- Web Workers

Tested in modern Chrome, Firefox, Safari, and Edge.

## Integration with Gradbot demos

Copy or symlink files to your demo's static directory:

```bash
# From your demo directory
ln -s ../../js_audio_processor static/js
```

Then update your HTML to use the correct path:
```javascript
const processor = new AudioProcessor({
  basePath: '/static/js',
  // ...
});
```
