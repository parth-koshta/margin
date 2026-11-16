// Prints the text of a PDF, one page per form feed, using macOS's own PDFKit (nothing to install).
// Usage: pdftext [--layout] FILE   (the password, if any, is read from stdin so it never shows in the process list)
// --layout rebuilds each line from the characters' positions, so a table's row stays on one line (PDFKit's plain
// text reads some tables column by column).
// Exit codes: 0 ok, 2 wrong or missing password, 3 not a readable PDF.
import Foundation
import PDFKit

var args = Array(CommandLine.arguments.dropFirst())
let layout = args.first == "--layout"
if layout { args.removeFirst() }
guard args.count == 1, let doc = PDFDocument(url: URL(fileURLWithPath: args[0])) else {
    FileHandle.standardError.write("not a readable PDF\n".data(using: .utf8)!)
    exit(3)
}
if doc.isLocked {
    let input = FileHandle.standardInput.readDataToEndOfFile()
    let password = (String(data: input, encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    if password.isEmpty || !doc.unlock(withPassword: password) {
        FileHandle.standardError.write("wrong or missing password\n".data(using: .utf8)!)
        exit(2)
    }
}

func layoutText(_ page: PDFPage) -> String {
    // PDFKit's own line selections keep each cell's text together; lines sitting at the same height are one row.
    guard let all = page.selection(for: page.bounds(for: .mediaBox)) else { return "" }
    struct Piece { let text: String; let x: CGFloat; let y: CGFloat; let h: CGFloat }
    var pieces: [Piece] = []
    for line in all.selectionsByLine() {
        let t = (line.string ?? "").replacingOccurrences(of: "\n", with: " ").trimmingCharacters(in: .whitespaces)
        if t.isEmpty { continue }
        let b = line.bounds(for: page)
        pieces.append(Piece(text: t, x: b.minX, y: b.midY, h: max(b.height, 1)))
    }
    var rows: [[Piece]] = []
    for p in pieces.sorted(by: { $0.y > $1.y }) {
        if let last = rows.last, let f = last.first, abs(f.y - p.y) <= min(f.h, p.h) * 0.45 {
            rows[rows.count - 1].append(p)
        } else {
            rows.append([p])
        }
    }
    return rows.map { $0.sorted(by: { $0.x < $1.x }).map { $0.text }.joined(separator: "   ") }.joined(separator: "\n")
}

var out = ""
for i in 0..<doc.pageCount {
    if i > 0 { out += "\u{0C}" }
    if let page = doc.page(at: i) {
        out += layout ? layoutText(page) : (page.string ?? "")
    }
}
FileHandle.standardOutput.write(out.data(using: .utf8)!)
