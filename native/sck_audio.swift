// Captura o áudio do sistema (ScreenCaptureKit, macOS 13+) e escreve PCM float32 intercalado, 48 kHz, 2 canais, no stdout.
// Saída: 0 = ok/parado, 2 = o stream caiu, 3 = sem permissão de gravação de tela/áudio, 4 = sem display.
// Compilar: swiftc -O native/sck_audio.swift -o sck_audio
import CoreMedia
import Foundation
import ScreenCaptureKit

let rate = 48_000, channels = 2

final class Sink: NSObject, SCStreamOutput, SCStreamDelegate {
    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        if ProcessInfo.processInfo.environment["SCK_DEBUG"] != nil { fputs("buf type=\(type.rawValue) n=\(sb.numSamples)\n", stderr) }
        guard type == .audio, sb.isValid, sb.numSamples > 0 else { return }
        try? sb.withAudioBufferList { abl, _ in
            let n = sb.numSamples
            var out = [Float](repeating: 0, count: n * channels)
            if abl.count >= channels {  // planar: um buffer por canal
                for c in 0..<channels {
                    guard let p = abl[c].mData?.assumingMemoryBound(to: Float.self) else { continue }
                    for i in 0..<min(n, Int(abl[c].mDataByteSize) / 4) { out[i * channels + c] = p[i] }
                }
            } else if let p = abl[0].mData?.assumingMemoryBound(to: Float.self) {  // já intercalado
                for i in 0..<min(n * channels, Int(abl[0].mDataByteSize) / 4) { out[i] = p[i] }
            }
            out.withUnsafeBytes { raw in
                if fwrite(raw.baseAddress, 1, raw.count, stdout) != raw.count { exit(0) }  // leitor morreu
            }
            fflush(stdout)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fputs("stream parou: \(error.localizedDescription)\n", stderr)
        exit(2)
    }
}

let sink = Sink()
var keep: SCStream?  // o SCStream precisa ficar vivo, senão o replayd não entrega o áudio
Task {
    do {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else { fputs("sem display\n", stderr); exit(4) }
        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.excludesCurrentProcessAudio = true
        cfg.sampleRate = rate
        cfg.channelCount = channels
        cfg.width = 128; cfg.height = 72  // vídeo mínimo: só queremos o áudio
        let stream = SCStream(filter: SCContentFilter(display: display, excludingWindows: []), configuration: cfg, delegate: sink)
        keep = stream
        try stream.addStreamOutput(sink, type: .audio, sampleHandlerQueue: DispatchQueue(label: "audio"))
        try stream.addStreamOutput(sink, type: .screen, sampleHandlerQueue: DispatchQueue(label: "video"))  // ignorado; sem ele o SCK reclama
        try await stream.startCapture()
        fputs("capturando\n", stderr)
    } catch {
        fputs("erro: \(error.localizedDescription)\n", stderr)
        exit(3)  // sem permissão é o caso comum
    }
}
signal(SIGPIPE, SIG_DFL)
RunLoop.main.run()
