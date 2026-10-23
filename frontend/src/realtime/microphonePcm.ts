import processorUrl from "./pcm16Processor.ts?worker&url";

type PcmChunkHandler = (chunk: ArrayBuffer) => void;

export class MicrophonePcmCapture {
  private active = false;
  private flushResolver?: () => void;

  private constructor(
    private readonly stream: MediaStream,
    private readonly context: AudioContext,
    private readonly source: MediaStreamAudioSourceNode,
    private readonly processor: AudioWorkletNode,
    private readonly onChunk: PcmChunkHandler,
  ) {
    processor.port.onmessage = ({ data }: MessageEvent<ArrayBuffer | { type?: string }>) => {
      if (data instanceof ArrayBuffer) {
        if (this.active) this.onChunk(data);
        return;
      }
      if (data?.type === "flushed") {
        this.flushResolver?.();
        this.flushResolver = undefined;
      }
    };
  }

  static async open(onChunk: PcmChunkHandler): Promise<MicrophonePcmCapture> {
    if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
      if (typeof window !== "undefined" && !window.isSecureContext) {
        throw new Error(
          "Trình duyệt chặn Micro vì trang web không bảo mật (HTTP qua IP mạng WiFi). Vui lòng dùng https:// hoặc cấu hình chrome://flags."
        );
      }
      throw new Error("Trình duyệt không hỗ trợ microphone hoặc thiết bị không có micro.");
    }

    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: { ideal: 1 },
        sampleRate: { ideal: 16_000 },
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    let context: AudioContext | undefined;
    try {
      context = new AudioContext({ sampleRate: 16_000, latencyHint: "interactive" });
      if (!context.audioWorklet) throw new Error("Trình duyệt không hỗ trợ AudioWorklet.");
      await context.audioWorklet.addModule(processorUrl);
      const source = context.createMediaStreamSource(stream);
      const processor = new AudioWorkletNode(context, "pcm16-capture", {
        numberOfInputs: 1,
        numberOfOutputs: 0,
        channelCount: 1,
        channelCountMode: "explicit",
      });
      source.connect(processor);
      await context.suspend();
      return new MicrophonePcmCapture(stream, context, source, processor, onChunk);
    } catch (error) {
      stream.getTracks().forEach((track) => track.stop());
      if (context) await context.close();
      throw error;
    }
  }

  async resume() {
    this.active = true;
    await this.context.resume();
  }

  async pause() {
    await this.flush();
    this.active = false;
    await this.context.suspend();
  }

  async stop(flush = true) {
    if (flush) await this.flush();
    this.active = false;
    this.flushResolver = undefined;
    this.source.disconnect();
    this.processor.disconnect();
    this.stream.getTracks().forEach((track) => track.stop());
    await this.context.close();
  }

  private async flush() {
    if (!this.active || this.context.state !== "running") return;
    await Promise.race([
      new Promise<void>((resolve) => {
        this.flushResolver = resolve;
        this.processor.port.postMessage({ type: "flush" });
      }),
      new Promise<void>((resolve) => window.setTimeout(resolve, 250)),
    ]);
    this.flushResolver = undefined;
  }
}
