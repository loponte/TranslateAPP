// Captura o áudio do sistema ou de um app só (ScreenCaptureKit, macOS 13+) e escreve PCM float32 intercalado, 48 kHz,
// 2 canais, no stdout.
//   sck_audio                   áudio do sistema
//   sck_audio --list            apps abertos, um por linha: "pid\tbundleID\tnome"; sai em seguida
//   sck_audio --app <bundleID>  só esse app e os processos "<bundleID>.*" (ex.: com.hnc.Discord.helper)
// Saída: 0 = ok/parado, 2 = o stream caiu, 3 = sem permissão de gravação de tela/áudio, 4 = sem display,
//        5 = app não encontrado, 6 = o app fechou (o Python reabre quando ele voltar).
// Compilar: swiftc -O native/sck_audio.swift -o sck_audio
import CoreMedia
import Foundation
import ScreenCaptureKit

let rate = 48_000, channels = 2
let args = CommandLine.arguments

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
        if args.contains("--list") {
            for a in content.applications where !a.bundleIdentifier.isEmpty {
                print("\(a.processID)\t\(a.bundleIdentifier)\t\(a.applicationName)")
            }
            exit(0)  // exit() descarrega o buffer do stdout
        }
        guard let display = content.displays.first else { fputs("sem display\n", stderr); exit(4) }
        var filter = SCContentFilter(display: display, excludingWindows: [])
        if let i = args.firstIndex(of: "--app"), i + 1 < args.count {
            let id = args[i + 1]  // o áudio de apps Chromium (Discord, Chrome) pode sair do processo "<id>.helper"
            let apps = content.applications.filter { $0.bundleIdentifier == id || $0.bundleIdentifier.hasPrefix(id + ".") }
            guard !apps.isEmpty else { fputs("app não encontrado: \(id)\n", stderr); exit(5) }
            filter = SCContentFilter(display: display, including: apps, exceptingWindows: [])
            let pids = apps.map { $0.processID }
            Thread.detachNewThread {  // todos os processos do app saíram -> 6
                while true {
                    sleep(1)
                    if pids.allSatisfy({ kill($0, 0) != 0 && errno == ESRCH }) { exit(6) }
                }
            }
        }
        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.excludesCurrentProcessAudio = true
        cfg.sampleRate = rate
        cfg.channelCount = channels
        cfg.width = 128; cfg.height = 72  // vídeo mínimo: só queremos o áudio
        let stream = SCStream(filter: filter, configuration: cfg, delegate: sink)
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
