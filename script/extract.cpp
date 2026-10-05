#include "extract.hpp"

#include <mupdf/fitz.h>
#include <mupdf/pdf.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <cctype>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <future>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <numeric>
#include <optional>
#include <queue>
#include <random>
#include <regex>
#include <set>
#include <sstream>
#include <string>
#include <thread>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <variant>
#include <vector>

namespace fs = std::filesystem;

namespace scholaret {

namespace {
inline float clampf(float v, float lo, float hi) noexcept {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

inline bool float_eq(float a, float b, float eps = 0.5f) noexcept {
    return std::fabs(a - b) <= eps;
}

inline std::string to_lower_copy(const std::string& s) {
    std::string t = s;
    std::transform(t.begin(), t.end(), t.begin(), [](unsigned char c) { return std::tolower(c); });
    return t;
}

inline void append_utf8(std::string& out, int cp) {
    if (cp < 0) return;
    unsigned int c = static_cast<unsigned int>(cp);
    if (c < 0x80) {
        out.push_back(static_cast<char>(c));
    } else if (c < 0x800) {
        out.push_back(static_cast<char>(0xC0 | (c >> 6)));
        out.push_back(static_cast<char>(0x80 | (c & 0x3F)));
    } else if (c < 0x10000) {
        out.push_back(static_cast<char>(0xE0 | (c >> 12)));
        out.push_back(static_cast<char>(0x80 | ((c >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (c & 0x3F)));
    } else {
        out.push_back(static_cast<char>(0xF0 | (c >> 18)));
        out.push_back(static_cast<char>(0x80 | ((c >> 12) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | ((c >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (c & 0x3F)));
    }
}

inline std::string trim_copy(const std::string& s) {
    if (s.empty()) return s;
    size_t l = 0;
    size_t r = s.size();
    while (l < r && std::isspace(static_cast<unsigned char>(s[l]))) ++l;
    while (r > l && std::isspace(static_cast<unsigned char>(s[r - 1]))) --r;
    return s.substr(l, r - l);
}

inline std::string collapse_whitespace(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    bool in_space = false;
    for (char c : s) {
        if (std::isspace(static_cast<unsigned char>(c))) {
            if (!in_space) {
                out.push_back(' ');
                in_space = true;
            }
        } else {
            out.push_back(c);
            in_space = false;
        }
    }
    return out;
}

inline bool contains_token(const std::string& hay, const std::vector<std::string>& tokens) {
    if (tokens.empty()) return false;
    std::string h = to_lower_copy(hay);
    for (const auto& t : tokens) {
        if (t.empty()) continue;
        if (h.find(to_lower_copy(t)) != std::string::npos) return true;
    }
    return false;
}

inline bool is_bullet_char(char c) noexcept {
    return c == '-' || c == '*' || c == '•' || c == '·' || c == '‣' || c == '○';
}

// ---------------------------------------------------------------------------
// LaTeX / math reconstruction helpers
// ---------------------------------------------------------------------------

// One decoded glyph taken from the structured-text page, before spans are built.
// Count ordinary English-looking words: runs of letters, at least two long.
// Units such as "m" or "s" do not count, which is what separates "at 3.0 m from
// O" from "25 m/s x 0.6".
inline int wordlike_count(const std::string& t) {
    int n = 0;
    size_t i = 0;
    while (i < t.size()) {
        if (std::isalpha(static_cast<unsigned char>(t[i]))) {
            size_t j = i;
            while (j < t.size() && std::isalpha(static_cast<unsigned char>(t[j]))) ++j;
            if (j - i >= 2) ++n;
            i = j;
        } else {
            ++i;
        }
    }
    return n;
}

// A span with real words in it is prose that happens to contain an operator,
// not an equation. Equations carry units and symbols, not verbs.
inline bool span_looks_like_prose(const std::string& t) {
    return wordlike_count(t) >= 3;
}

struct CharInfo {
    int ucs = 0;
    int gid = 0;
    fz_font* font = nullptr;
    float size = 0.0f;
    float baseline = 0.0f;
    fz_rect bbox = fz_empty_rect;
    std::string latex;
    bool is_math_symbol = false;
    // Set when ucs is U+FFFD: what the CID pass reported, kept so the glyph can
    // be named in diagnostics once the whole line is known.
    int unmapped_code = 0;
    std::string unmapped_name;
};

inline bool cp_is_math_symbol(int cp) {
    switch (cp) {
        case '=':
        case '+':
        case '-':
        case 0x2212:  // minus sign
        case 0x00D7:  // multiplication sign
        case 0x00F7:  // division sign
        case 0x00B1:  // plus-minus
        case 0x2248:  // almost equal
        case 0x2260:  // not equal
        case 0x2264:  // less-than or equal
        case 0x2265:  // greater-than or equal
        case 0x221A:  // square root
        case 0x221E:  // infinity
        case 0x2202:  // partial differential
        case 0x2211:  // n-ary summation
        case 0x220F:  // n-ary product
        case 0x222B:  // integral
        case 0x2192:  // rightwards arrow
        case 0x2190:  // leftwards arrow
        case 0x21D2:  // implies
        case 0x2208:  // element of
        case 0x2209:  // not element of
        case 0x2286:  // subset
        case 0x00B0:  // degree
        case 0x00B7:  // middle dot
        case '^':
        case '_':
            return true;
        default:
            break;
    }
    // Greek and letterlike symbols (ranges), which are math by nature.
    if (cp >= 0x0391 && cp <= 0x03C9) return true;   // Greek capitals + lowercase
    if (cp >= 0x2200 && cp <= 0x22FF) return true;   // mathematical operators
    if (cp >= 0x27C0 && cp <= 0x27EF) return true;   // misc mathematical symbols A
    if (cp >= 0x2100 && cp <= 0x214F) return true;   // letterlike symbols
    if (cp == 0x00B0 || cp == 0x2013 || cp == 0x2014) return true;
    return false;
}

inline bool font_is_math(const std::string& font_name_lower) {
    static const char* kTokens[] = {
        "symbol", "math", "cmmi", "cmsy", "cmex", "cmr", "msam", "msbm",
        "mtmi", "mt-extra", "mte", "euclid", "wasy", "stix", "asana", "xits",
        "texgyre", "lmmi", "lmsy", "lmex", "palatino-math", "dejavu-math",
    };
    for (const char* t : kTokens) {
        if (font_name_lower.find(t) != std::string::npos) return true;
    }
    return false;
}

inline float math_symbol_ratio(const std::string& s) {
    if (s.empty()) return 0.0f;
    int total = 0, sym = 0;
    size_t i = 0;
    while (i < s.size()) {
        unsigned char b = static_cast<unsigned char>(s[i]);
        int cp = b;
        int len = 1;
        if (b >= 0xF0) { cp = b & 0x07; len = 4; }
        else if (b >= 0xE0) { cp = b & 0x0F; len = 3; }
        else if (b >= 0xC0) { cp = b & 0x1F; len = 2; }
        for (int k = 1; k < len && i + static_cast<size_t>(k) < s.size(); ++k) {
            cp = (cp << 6) | (static_cast<unsigned char>(s[i + static_cast<size_t>(k)]) & 0x3F);
        }
        if (cp != ' ' && cp != 0) {
            ++total;
            if (cp_is_math_symbol(cp)) ++sym;
        }
        i += static_cast<size_t>(len);
    }
    return total > 0 ? static_cast<float>(sym) / static_cast<float>(total) : 0.0f;
}

// True when the span reads like a standalone quantity, e.g. "15 m/s", "9.8 N/kg".
inline bool looks_like_quantity(const std::string& s) {
    static const std::regex re(
        R"RX(^\(?-?[0-9]+([.,][0-9]+)?\s*(x|\^)?\s*([a-zA-Z%°]+(/[a-zA-Z]+)*)(\^[0-9]+)?\)?$)RX");
    if (s.size() > 24) return false;
    if (s.find_first_of("abcdefghijklmnopqrstuvwxyz") == std::string::npos) return false;
    return std::regex_match(s, re);
}

inline bool span_looks_like_math(const std::string& text, const std::string& font_lower) {
    if (text.empty()) return false;
    if (font_is_math(font_lower)) return true;
    if (text.find('=') != std::string::npos) return true;
    if (math_symbol_ratio(text) >= 0.6f) return true;
    if (looks_like_quantity(text)) return true;
    return false;
}

// Map an Adobe/PostScript glyph name onto LaTeX markup. Returns empty when unknown.
inline std::string glyph_name_to_latex(const std::string& name) {
    if (name.empty()) return std::string();
    if (name == "space" || name == "spacehack" || name == "softhyphen") return " ";

    // uniXXXX / uXXXX[XX] style names carry the real code point.
    if (name.size() > 3 && (name.compare(0, 3, "uni") == 0 || name[0] == 'u')) {
        size_t start = (name[0] == 'u' && name.compare(0, 3, "uni") != 0) ? 1 : 3;
        std::string hexpart = name.substr(start);
        bool ok = !hexpart.empty() && hexpart.size() <= 6;
        for (char c : hexpart) {
            if (!std::isxdigit(static_cast<unsigned char>(c))) { ok = false; break; }
        }
        if (ok) {
            unsigned int cp = static_cast<unsigned int>(std::strtoul(hexpart.c_str(), nullptr, 16));
            if (cp > 0 && cp <= 0x10FFFF) {
                std::string out;
                append_utf8(out, static_cast<int>(cp));
                return out;
            }
        }
    }

    struct Entry { const char* n; const char* r; };
    static const Entry kTable[] = {
        {"radical", "\xE2\x88\x9A"}, {"radicalex", "\xE2\x88\x9A"},
        {"multiply", "\\times"}, {"asteriskmath", "\\cdot"},
        {"dotmath", "\\cdot"}, {"periodcentered", "\\cdot"},
                {"plusminus", "\\pm"}, {"minusplus", "\\mp"},
                        {"notequal", "\\neq"}, {"approxequal", "\\approx"},
        {"approximatelyequal", "\\approx"}, {"similar", "\\sim"},
        {"congruent", "\\cong"}, {"propersimilar", "\\simeq"},
        {"lessequal", "\\leq"}, {"greaterequal", "\\geq"},
        {"lessequalequal", "\\leqq"}, {"greaterequalequal", "\\geqq"},
        {"lessorequalslant", "\\leqslant"}, {"greaterorequalslant", "\\geqslant"},
        {"element", "\\in"}, {"notelement", "\\notin"},
        {"propersubset", "\\subset"}, {"reflexsubset", "\\subseteq"},
        {"propersupset", "\\supset"}, {"reflexsuperset", "\\superseteq"},
        {"notsubset", "\\not\\subset"},
        {"union", "\\cup"}, {"intersection", "\\cap"},
        {"emptyset", "\\varnothing"}, {"universal", "\\forall"},
        {"existential", "\\exists"}, {"logicalnot", "\\neg"},
        {"logicaland", "\\wedge"}, {"logicalor", "\\vee"},
        {"infinity", "\\infty"}, {"partialdiff", "\\partial"},
        {"summation", "\\sum"}, {"summationtext", "\\sum"},
        {"summationdisplay", "\\sum"},
        {"product", "\\prod"}, {"integral", "\\int"},
        {"integraldisplay", "\\int"}, {"radicalbigg", "\xE2\x88\x9A"},
        {"angleleft", "\\langle"}, {"angleright", "\\rangle"},
        {"arrowright", "\\rightarrow"}, {"arrowleft", "\\leftarrow"},
        {"arrowup", "\\uparrow"}, {"arrowdown", "\\downarrow"},
        {"arrowboth", "\\leftrightarrow"},
        {"arrowdblright", "\\Rightarrow"}, {"arrowdblleft", "\\Leftarrow"},
        {"arrowdblboth", "\\Leftrightarrow"},
        {"implies", "\\Rightarrow"}, {"if", "\\Rightarrow"},
        {"degree", "^{\\circ}"},                 {"angle", "\\angle"}, {"triangle", "\\triangle"},
        {"therefore", "\\therefore"}, {"because", "\\because"},
        {"circleplus", "\\oplus"}, {"circlemultiply", "\\otimes"},
        {"ellipsis", "\\ldots"},                 {"dagger", "\\dagger"}, {"daggerdbl", "\\ddagger"},
                    };
    for (const Entry& e : kTable) {
        if (name == e.n) return e.r;
    }
    return std::string();
}

// Resolve a character code to LaTeX via the standard encoding tables.
// Sets *is_math_repl when the code denotes a mathematical symbol (radical,
// times, plusminus, ...) so the caller can mark the span as math.
// Returns an empty string when the code is not a math symbol.

// Render the text around an unmapped glyph so the diagnostics show what it was.
inline std::string char_context(const std::vector<CharInfo>& chars,
                                const CharInfo& cur, size_t idx) {
    auto cp_text = [](int cp) -> std::string {
        if (cp <= 0) return std::string();
        std::string t;
        append_utf8(t, cp);
        return t;
    };
    std::string before;
    for (size_t i = (idx >= 7 ? idx - 7 : 0); i < idx; ++i) before += cp_text(chars[i].ucs);
    std::string after;
    for (size_t i = idx + 1; i < chars.size() && i < idx + 7; ++i) after += cp_text(chars[i].ucs);
    return "[" + before + "|" + cur.latex + "|?" + after + "]";
}

// Sound unicode -> LaTeX conversion, for characters MuPDF mapped correctly.
// Deliberately does NOT consult the encoding tables: those are indexed by byte
// code within a single encoding, not by unicode, so looking up U+00A0 there
// yields MacRoman's "degree" and turned every non-breaking space into ^\circ.
inline std::string unicode_to_latex(int cp, bool* is_math_repl) {
    switch (cp) {
        case 0x00D7: *is_math_repl = true; return "\\times";   // multiplication sign
        case 0x00F7: *is_math_repl = true; return "\\div";     // division sign
        case 0x00B1: *is_math_repl = true; return "\\pm";      // plus-minus
        case 0x00B0: *is_math_repl = true; return "^{\\circ}"; // degree
        case 0x2264: *is_math_repl = true; return "\\leq";
        case 0x2265: *is_math_repl = true; return "\\geq";
        case 0x2260: *is_math_repl = true; return "\\neq";
        case 0x2248: *is_math_repl = true; return "\\approx";
        case 0x221E: *is_math_repl = true; return "\\infty";
        case 0x2202: *is_math_repl = true; return "\\partial";
        case 0x220F: *is_math_repl = true; return "\\prod";
        case 0x222B: *is_math_repl = true; return "\\int";
        case 0x00B7: *is_math_repl = true; return "\\cdot";    // middle dot
        case 0x2212: *is_math_repl = true; return "-";          // minus sign
        default: break;
    }
    *is_math_repl = false;
    return std::string();
}

// Recover what an undecodable glyph actually is.
//
// Pass B hands back the raw character code, and for these subset fonts that is
// the glyph id -- a font-local index whose meaning depends entirely on the font,
// which is why reading it through the standard encoding tables produced nonsense
// (a solidus came out as a pilcrow; codes 61 and 70 both looked like "=").
//
// MuPDF can name a glyph directly, so do that: gid -> Adobe glyph name, and the
// name gives either a unicode value or a LaTeX form. When neither resolves the
// glyph stays U+FFFD and is reported with its context.
//
// For this textbook 63 glyphs resolve no further: fz_get_glyph_name returns an
// empty name and no tested unicode maps to the id via fz_encode_character, so
// the subset fonts carry neither an Adobe name nor a usable cmap entry for them.
// The character code is simply not in the file, so only OCR could recover it.
// resolve_glyph() still earns its place on documents that do have names.
struct GlyphGuess {
    int ucs = 0;              // resolved unicode, when the name is AGL-mappable
    std::string latex;        // otherwise a LaTeX form, when we know one
    std::string name;         // glyph name, for diagnostics
};

inline GlyphGuess resolve_glyph(fz_context* ctx, fz_font* font, int gid) {
    GlyphGuess out;
    if (!ctx || !font || gid <= 0) return out;
    char buf[128] = {0};
    fz_get_glyph_name(ctx, font, gid, buf, sizeof(buf));
    out.name = buf;
    if (buf[0] == '.' || buf[0] == 0) return out;  // .notdef or unnamed
    int u = fz_unicode_from_glyph_name(buf);
    if (u > 0) {
        out.ucs = u;
        return out;
    }
    out.latex = glyph_name_to_latex(out.name);
    return out;
}

// Keep LaTeX commands from fusing with the text that follows them.
//
// "\timesP" is a single control sequence named "timesP", not "\times" followed
// by P, so a renderer either errors or prints the literal text. A space ends the
// command name and is invisible in math mode. Only needed when the very next
// character is a letter, and only for the commands this file emits.
inline std::string separate_latex_commands(const std::string& s) {
    static const char* const kCmds[] = {
        "times", "div", "pm", "mp", "leq", "geq", "neq", "approx", "sim",
        "infty", "partial", "prod", "sum", "int", "cdot", "times",
        "langle", "rangle", "rightarrow", "leftarrow", "leftrightarrow",
        "varnothing", "forall", "exists", "neg", "wedge", "vee",
        "oplus", "otimes", "ldots", "dagger", "ddagger", "angle",
        "triangle", "therefore", "because", "sqrt",
    };
    std::string out;
    out.reserve(s.size() + 8);
    size_t i = 0;
    while (i < s.size()) {
        if (s[i] == '\\') {
            // Greedy scan collects "timesP" as one name, which matches nothing in
            // kCmds. The name is the longest known command that is a prefix of that
            // run: for "\timesP" that is "times", leaving P as the next character.
            size_t j = i + 1;
            while (j < s.size() && std::isalpha(static_cast<unsigned char>(s[j]))) ++j;
            const size_t run_end = j;
            size_t cmd_len = 0;
            for (const char* c : kCmds) {
                const size_t n = std::strlen(c);
                if (n <= run_end - (i + 1) &&
                    s.compare(i + 1, n, c) == 0 && n > cmd_len) {
                    cmd_len = n;
                }
            }
            if (cmd_len == 0) {            // not a command we emit: copy verbatim
                out += s.substr(i, run_end - i);
                i = run_end;
                continue;
            }
            // cmd_len counts the name only; the backslash is a separate
            // character, so the consumed length is cmd_len + 1.
            out += s.substr(i, cmd_len + 1);
            i += cmd_len + 1;
            if (i < s.size() && std::isalpha(static_cast<unsigned char>(s[i]))) {
                out.push_back(' ');         // terminate the control sequence
            }
            continue;
        }
        out.push_back(s[i]);
        ++i;
    }
    return out;
}

// Turn bare radical signs into \sqrt{...} groups inside an already-math string.
inline std::string apply_radicals(const std::string& s) {
    const std::string kRadical = "\xE2\x88\x9A";
    std::string out;
    out.reserve(s.size() + 16);
    size_t i = 0;
    while (i < s.size()) {
        if (s.compare(i, kRadical.size(), kRadical) == 0) {
            size_t j = i + kRadical.size();
            while (j < s.size() && s[j] == ' ') ++j;
            size_t k = j;
            int depth = 0;
            while (k < s.size()) {
                char c = s[k];
                if (c == '{' || c == '(' || c == '[') {
                    ++depth;
                } else if (c == '}' || c == ')' || c == ']') {
                    if (depth == 0) break;
                    --depth;
                } else if (depth == 0 && (c == ',' || c == '=')) {
                    break;
                }
                ++k;
            }
            std::string inner = trim_copy(s.substr(j, k - j));
            if (!inner.empty()) {
                out += "\\sqrt{";
                out += inner;
                out += "}";
                i = k;
                continue;
            }
            out += "\\sqrt{}";
            i += kRadical.size();
            continue;
        }
        out.push_back(s[i]);
        ++i;
    }
    return out;
}

inline std::string slugify(const std::string& s) {
    std::string o;
    o.reserve(s.size());
    bool last_dash = true;
    for (unsigned char c : s) {
        if (std::isalnum(c)) {
            o.push_back(static_cast<char>(std::tolower(c)));
            last_dash = false;
        } else {
            if (!last_dash) {
                o.push_back('-');
                last_dash = true;
            }
        }
    }
    if (!o.empty() && o.back() == '-') o.pop_back();
    if (o.empty()) o = "section";
    return o;
}

inline std::string num_to_str(int n) {
    std::ostringstream ss;
    ss << n;
    return ss.str();
}

inline std::string uint32_to_str(uint32_t n) {
    std::ostringstream ss;
    ss << n;
    return ss.str();
}
} // anon

ThreadPool::ThreadPool(int threads) {
    int tc = threads;
    if (tc <= 0) {
        tc = static_cast<int>(std::thread::hardware_concurrency());
    }
    if (tc < 1) tc = 1;
    if (tc > 256) tc = 256;
    workers_.reserve(static_cast<size_t>(tc));
    for (int i = 0; i < tc; ++i) {
        workers_.emplace_back([this]() {
            while (true) {
                std::function<void()> task;
                {
                    std::unique_lock<std::mutex> lock(this->mutex_);
                    this->cv_.wait(lock, [this]() {
                        return this->stop_ || !this->tasks_.empty();
                    });
                    if (this->stop_ && this->tasks_.empty()) break;
                    task = std::move(this->tasks_.front());
                    this->tasks_.pop();
                    ++this->active_;
                }
                if (task) task();
                {
                    std::lock_guard<std::mutex> lock(this->mutex_);
                    --this->active_;
                }
                this->cv_.notify_all();
            }
        });
    }
}

ThreadPool::~ThreadPool() {
    stop();
}

void ThreadPool::stop() {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        stop_ = true;
    }
    cv_.notify_all();
    for (auto& t : workers_) {
        if (t.joinable()) t.join();
    }
    workers_.clear();
    while (!tasks_.empty()) tasks_.pop();
}

void ThreadPool::wait_all() {
    std::unique_lock<std::mutex> lock(mutex_);
    cv_.wait(lock, [this]() {
        return tasks_.empty() && active_.load() == 0;
    });
}

size_t ThreadPool::size() const noexcept {
    std::lock_guard<std::mutex> lock(mutex_);
    return tasks_.size();
}

bool ThreadPool::empty() const noexcept {
    std::lock_guard<std::mutex> lock(mutex_);
    return tasks_.empty() && active_.load() == 0;
}

Extractor::Extractor(const ExtractConfig& config)
    : config_(config) {}

Extractor::~Extractor() {
    cleanup_context();
}

bool Extractor::initialize_context() {
    if (ctx_) return true;
    fz_context* c = fz_new_context(nullptr, nullptr, FZ_STORE_UNLIMITED);
    if (!c) return false;
    fz_register_document_handlers(c);
    ctx_ = std::unique_ptr<fz_context, std::function<void(fz_context*)>>(
        c, [](fz_context* p) { if (p) fz_drop_context(p); });
    return true;
}

void Extractor::cleanup_context() {
    ctx_.reset();
}

RectF to_rectf(const fz_rect& r) {
    RectF rf;
    rf.x0 = r.x0; rf.y0 = r.y0; rf.x1 = r.x1; rf.y1 = r.y1;
    return rf;
}

fz_rect to_fzrect(const RectF& rf) {
    fz_rect fr;
    fr.x0 = rf.x0; fr.y0 = rf.y0; fr.x1 = rf.x1; fr.y1 = rf.y1;
    return fr;
}

std::vector<std::string> Extractor::read_batch_list(const std::filesystem::path& list_path) {
    std::vector<std::string> out;
    std::ifstream f(list_path);
    if (!f.is_open()) return out;
    std::string line;
    while (std::getline(f, line)) {
        line = trim_copy(line);
        if (line.empty() || line[0] == '#') continue;
        out.push_back(line);
    }
    return out;
}

void Extractor::ensure_directories() {
    try {
        if (!config_.output_dir.empty()) fs::create_directories(config_.output_dir);
        if (!config_.assets_dir.empty()) fs::create_directories(config_.assets_dir);
    } catch (...) {}
}

DocResult Extractor::extract_single(const std::filesystem::path& input_path) {
    if (!initialize_context()) {
        DocResult dr;
        dr.input_path = input_path.string();
        dr.success = false;
        dr.errors.push_back("Failed to initialize MuPDF context");
        return dr;
    }
    return process_document(input_path);
}

std::vector<DocResult> Extractor::extract_batch() {
    std::vector<DocResult> results;
    std::vector<std::string> list = read_batch_list(config_.batch_list);
    if (list.empty()) return results;
    ThreadPool pool(config_.threads);
    std::vector<std::future<DocResult>> futs;
    futs.reserve(list.size());
    for (const auto& s : list) {
        futs.emplace_back(pool.enqueue([this, s]() {
            return extract_single(fs::path(s));
        }));
    }
    pool.wait_all();
    results.reserve(futs.size());
    for (auto& f : futs) {
        try { results.push_back(f.get()); } catch (...) {}
    }
    return results;
}

DocResult Extractor::process_document(const std::filesystem::path& input_path) {
    DocResult dr;
    dr.input_path = input_path.string();
    auto t0 = std::chrono::high_resolution_clock::now();
    ensure_directories();
    if (!initialize_context()) {
        dr.success = false;
        dr.errors.push_back("MuPDF context init failed");
        return dr;
    }
    fz_context* c = ctx_.get();
    fz_document* doc = nullptr;
    std::string in_str = input_path.string();
    fz_try(c) {
        doc = fz_open_document(c, in_str.c_str());
    } fz_catch(c) {
        dr.success = false;
        dr.errors.push_back("fz_open_document failed");
        return dr;
    }
    int n = fz_count_pages(c, doc);
    dr.total_pages = static_cast<size_t>(n);
    std::vector<PageResult> page_results(static_cast<size_t>(n));
    ThreadPool pool(config_.threads);
    std::vector<std::future<bool>> futs;
    futs.clear();
    futs.reserve(static_cast<size_t>(n));
    for (int p = 0; p < n; ++p) {
        futs.emplace_back(pool.enqueue([this, c, doc, p, &page_results]() -> bool {
            std::lock_guard<std::mutex> l(ctx_mutex_);
            fz_page* page = nullptr;
            fz_try(c) { page = fz_load_page(c, doc, p); } fz_catch(c) { return false; }
            PageResult pr = process_page(doc, p + 1, page);
            page_results[static_cast<size_t>(p)] = std::move(pr);
            fz_drop_page(c, page);
            return true;
        }));
    }
    pool.wait_all();
    for (auto& ff : futs) {
        try { (void)ff.get(); } catch (...) {}
    }
    fz_drop_document(c, doc);
    dr.pages = std::move(page_results);
    dr.merged_blocks = merge_document_blocks(dr.pages);
    drop_repeated_page_text(dr.merged_blocks, dr.pages.size());
    normalize_blocks(dr.merged_blocks);
    build_outline(dr.merged_blocks, dr);
    for (const auto& pr : dr.pages) {
        dr.total_blocks += pr.blocks.size();
        dr.total_images += pr.images.size();
        for (const auto& im : pr.images) dr.all_images.push_back(im);
    }
    std::string md_out = config_.output_md.string();
    if (md_out.empty()) {
        md_out = (config_.output_dir / config_.md_filename).string();
    }
    dr.output_md_path = md_out;
    dr.assets_dir = config_.assets_dir.string();
    render_markdown(dr, fs::path(md_out));
    dump_diagnostics();
    auto t1 = std::chrono::high_resolution_clock::now();
    dr.elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0);
    dr.success = true;
    return dr;
}

PageResult Extractor::process_page(fz_document* doc, int page_num, fz_page* page) {
    (void)doc;
    PageResult pr;
    pr.page_num = page_num;
    pr.processed = false;
    fz_context* c = ctx_.get();
    fz_rect pb = fz_bound_page(c, page);
    pr.page_bbox = to_rectf(pb);
    pr.page_width = pb.x1 - pb.x0;
    pr.page_height = pb.y1 - pb.y0;
    extract_text_from_page(page, pr);
    if (config_.extract_images) {
        extract_images_from_page(page, pr);
    }
    pr.blocks = analyze_layout(pr.lines, pr);
    detect_columns(pr);
    detect_headers_footers(pr, pr.blocks);
    classify_blocks(pr.blocks, pr.stats, page_num);
    refine_headings(pr.blocks, pr.stats);
    detect_lists(pr.blocks);
    attach_captions(pr.blocks, pr.images);
    pr.processed = true;
    return pr;
}

void Extractor::extract_text_from_page(fz_page* page, PageResult& pr) {
    fz_context* c = ctx_.get();

    // The page is extracted twice on purpose:
    //   pass A (no CID flag) gives the true unicode, and yields U+FFFD for every
    //          glyph MuPDF could not map;
    //   pass B (CID flag) gives the raw code for those same positions.
    // Comparing them position by position tells us exactly which glyphs were
    // unmapped, so we can name them through the encoding tables instead of
    // guessing -- and we never mistake a mapped character for a raw code.
    auto run_stext = [&](fz_stext_page** out, int flags) -> bool {
        bool ok = false;
        fz_try(c) {
            fz_stext_options opts;
            std::memset(&opts, 0, sizeof(opts));
            opts.flags = flags;
            *out = fz_new_stext_page(c, fz_bound_page(c, page));
            fz_device* dev = fz_new_stext_device(c, *out, &opts);
            fz_run_page(c, page, dev, fz_identity, nullptr);
            fz_drop_device(c, dev);
            ok = true;
        } fz_catch(c) {
            ok = false;
        }
        return ok;
    };

    fz_stext_page* stp = nullptr;
    fz_stext_page* cidp = nullptr;
    if (!run_stext(&stp, 0)) {
        if (stp) fz_drop_stext_page(c, stp);
        return;
    }
    if (!run_stext(&cidp, FZ_STEXT_USE_CID_FOR_UNKNOWN_UNICODE)) {
        if (cidp) fz_drop_stext_page(c, cidp);
        cidp = nullptr;
    }
    std::vector<Line> lines_list;
    std::vector<float> fsizes_all;
    // Walk both passes line by line so char i of pass A lines up with char i of
    // pass B. Bail out of the merge if the two ever disagree in length.
    fz_stext_block* cidblk = cidp ? cidp->first_block : nullptr;
    fz_stext_block* blk = stp->first_block;
    while (blk) {
        if (blk->type == FZ_STEXT_BLOCK_TEXT) {
            fz_stext_line* cidln = (cidblk && cidblk->type == FZ_STEXT_BLOCK_TEXT)
                                       ? cidblk->u.t.first_line : nullptr;
            fz_stext_line* ln = blk->u.t.first_line;
            while (ln) {
                Line l;
                l.bbox = to_rectf(ln->bbox);
                l.baseline_y = ln->first_char ? ln->first_char->origin.y : ln->bbox.y1;
                l.line_height = ln->bbox.y1 - ln->bbox.y0;
                fz_stext_char* ch = ln->first_char;
                fz_stext_char* cidch = cidln ? cidln->first_char : nullptr;
                std::vector<CharInfo> chars;
                while (ch) {
                    CharInfo ci;
                    ci.ucs = ch->c;
                    ci.gid = 0;
                    ci.font = ch->font;
                    ci.size = ch->size;
                    ci.baseline = ch->origin.y;
                    ci.bbox = fz_rect_from_quad(ch->quad);
                    if (ch->c == 0xFFFD || ch->c == 0) {
                        // Unmapped in pass A. Pass B hands us the raw code, but
                        // nothing here says which encoding the subset font uses,
                        // and guessing from the standard tables yields wrong
                        // characters: it reported "degree" and "pilcrow" where
                        // the glyphs were really "\times" and "/". So only an
                        // explicitly confirmed mapping is honoured; the rest stay
                        // U+FFFD and are reported with their context below.
                        int code = cidch ? cidch->c : 0;
                        const GlyphGuess g =
                            (code > 0 && code != 0xFFFD)
                                ? resolve_glyph(c, ci.font, code)
                                : GlyphGuess();
                        if (g.ucs > 0) {
                            ci.ucs = g.ucs;
                            ++stat_glyph_fixed_;
                        } else if (!g.latex.empty()) {
                            ci.ucs = -1;
                            ci.latex = g.latex;
                            ci.is_math_symbol = true;
                            ++stat_glyph_fixed_;
                        } else {
                            ci.ucs = 0xFFFD;
                            ci.unmapped_code = code;
                            ci.unmapped_name = g.name;
                        }
                        if (cidch) cidch = cidch->next;
                    }
                    chars.push_back(ci);
                    ch = ch->next;
                }
                if (cidln) cidln = cidln->next;

                // Now that every char on the line is known, report unmapped
                // glyphs with context on both sides.
                for (size_t i = 0; i < chars.size(); ++i) {
                    if (chars[i].ucs != 0xFFFD && chars[i].ucs != 0) continue;
                    note_unmapped_glyph(
                        "code=" + num_to_str(chars[i].unmapped_code) +
                        " ctx=" + char_context(chars, chars[i], i));
                }

                float dom_size = 0.0f;
                float dom_base = 0.0f;
                if (!chars.empty()) {
                    std::vector<float> sz;
                    sz.reserve(chars.size());
                    for (const auto& x : chars) sz.push_back(x.size);
                    std::sort(sz.begin(), sz.end());
                    dom_size = sz[sz.size() / 2];
                    std::vector<float> bl;
                    for (const auto& x : chars) {
                        if (std::fabs(x.size - dom_size) <= 0.05f * dom_size) bl.push_back(x.baseline);
                    }
                    if (!bl.empty()) {
                        std::sort(bl.begin(), bl.end());
                        dom_base = bl[bl.size() / 2];
                    }
                }

                auto script_of = [&](size_t idx) -> int {
                    if (dom_size <= 0.01f) return 0;
                    const CharInfo& x = chars[idx];
                    float rel = x.size / dom_size;
                    if (rel >= 0.85f) return 0;
                    float dy = x.baseline - dom_base;
                    if (dy < -0.15f * dom_size) return 1;
                    if (dy > 0.08f * dom_size) return 2;
                    // Faked superscript: smaller font sitting on the same baseline,
                    // immediately after a non-space glyph with no horizontal gap.
                    if (rel < 0.78f && idx > 0) {
                        const CharInfo& p = chars[idx - 1];
                        bool prev_nonspace = (p.ucs != ' ' && p.ucs != 0);
                        float gap = x.bbox.x0 - p.bbox.x1;
                        if (prev_nonspace && gap <= 0.6f) return 1;
                    }
                    return 0;
                };

                for (size_t ci = 0; ci < chars.size();) {
                    const CharInfo head = chars[ci];
                    const int script = script_of(ci);
                    size_t cj = ci;
                    std::string txt;
                    bool saw_math_symbol = false;
                    fz_rect sb = head.bbox;
                    while (cj < chars.size()) {
                        const CharInfo& x = chars[cj];
                        if (x.font != head.font) break;
                        if (!float_eq(x.size, head.size, 0.01f)) break;
                        if (script_of(cj) != script) break;
                        if (x.ucs < 0 && !x.latex.empty()) {
                            // Glyph recovered from the CID pass.
                            txt += x.latex;
                            if (x.is_math_symbol) saw_math_symbol = true;
                        } else if (x.ucs == 0 || x.ucs == 0xFFFD) {
                            // Already reported with context by the char loop.
                            txt += "\xEF\xBF\xBD";
                        } else {
                            // Already decoded correctly: use the unicode
                            // directly. Never run these through the encoding
                            // tables, which are indexed by byte code.
                            bool mrepl = false;
                            std::string rep = unicode_to_latex(x.ucs, &mrepl);
                            if (mrepl) {
                                txt += rep;
                                saw_math_symbol = true;
                            } else {
                                append_utf8(txt, x.ucs);
                            }
                        }
                        if (x.bbox.x0 < sb.x0) sb.x0 = x.bbox.x0;
                        if (x.bbox.y0 < sb.y0) sb.y0 = x.bbox.y0;
                        if (x.bbox.x1 > sb.x1) sb.x1 = x.bbox.x1;
                        if (x.bbox.y1 > sb.y1) sb.y1 = x.bbox.y1;
                        ++cj;
                    }
                    if (cj == ci) { ++ci; continue; }
                    Span s;
                    s.text = txt;
                    s.bbox = to_rectf(sb);
                    if (head.font) {
                        const char* fn = fz_font_name(c, head.font);
                        if (fn) s.font_name_raw = fn;
                        s.font_family = s.font_name_raw;
                    }
                    s.font_size = head.size;
                    s.font_weight = 0.0f;
                    std::string fnl = to_lower_copy(s.font_name_raw);
                    s.is_bold = (fnl.find("bold") != std::string::npos);
                    s.is_italic = (fnl.find("italic") != std::string::npos || fnl.find("oblique") != std::string::npos);
                    s.is_monospace = (fnl.find("mono") != std::string::npos || fnl.find("courier") != std::string::npos || fnl.find("typewriter") != std::string::npos);
                    s.is_serif = (fnl.find("serif") != std::string::npos);
                    s.is_sans = (fnl.find("sans") != std::string::npos);
                    if (s.is_bold) s.font_weight *= 1.05f;
                    s.script = script;
                    if (!s.text.empty() && s.font_size > 0.01f) {
                        const char* rule = "none";
                        if (saw_math_symbol) rule = "glyph";
                        else if (script != 0) rule = "script";
                        else if (font_is_math(fnl)) rule = "font";
                        else if (s.text.find('=') != std::string::npos) rule = "equals";
                        else if (math_symbol_ratio(s.text) >= 0.6f) rule = "ratio";
                        else if (looks_like_quantity(s.text)) rule = "quantity";
                        if (rule[0] != 'n' && span_looks_like_prose(s.text)) {
                            // Keep the rule for diagnostics, but do not emit the
                            // prose as math.
                            ++prose_rejected_;
                            if (math_rule_samples_.size() < 24) {
                                math_rule_samples_.push_back(std::string("rejected-prose | ") +
                                                             fnl + " | " + s.text.substr(0, 40));
                            }
                            rule = "none";
                        }
                        s.is_math = (rule[0] != 'n');
                        if (s.is_math) {
                            std::lock_guard<std::mutex> lk(diag_mutex_);
                            ++math_rules_[rule];
                            if (math_rule_samples_.size() < 24) {
                                math_rule_samples_.push_back(std::string(rule) + " | " +
                                                             fnl + " | " + s.text.substr(0, 40));
                            }
                        }
                        if (s.is_math) ++stat_math_spans_;
                        // Span-level trace for a pattern, enabled with
                        // SCHOLARET_TRACE_SPANS="gauge|obj|Pamt". Answers "why is
                        // this subscript split" without needing the artifact
                        // locally.
                        {
                            const char* pat = std::getenv("SCHOLARET_TRACE_SPANS");
                            if (pat && *pat) {
                                // "a|b|c" means any of a, b or c, not a literal.
                                std::string p2 = pat;
                                std::string best;
                                for (size_t a = 0; a <= p2.size(); ++a) {
                                    if (a != p2.size() && p2[a] != '|') continue;
                                    const std::string alt = p2.substr(0, a);
                                    p2.erase(0, a + (a < p2.size() ? 1 : 0));
                                    a = (size_t)-1;
                                    if (alt.empty()) continue;
                                    if (txt.find(alt) != std::string::npos && alt.size() > best.size()) {
                                        best = alt;
                                    }
                                }
                                if (!best.empty()) {
                                    std::cerr << "[SPANTRACE] hit=" << best
                                              << " script=" << script
                                              << " size=" << s.font_size
                                              << " math=" << (s.is_math ? 1 : 0)
                                              << " rule=" << rule
                                              << " text=[" << probe << "]\n";
                                }
                            }
                        }
                        if (script == 1) ++stat_sup_spans_;
                        else if (script == 2) ++stat_sub_spans_;
                        l.spans.push_back(std::move(s));
                    }
                    ci = cj;
                }
                if (!l.spans.empty()) {
                    lines_list.push_back(std::move(l));
                }
                ln = ln->next;
            }
        }
        blk = blk->next;
        if (cidblk) cidblk = cidblk->next;
    }
    for (auto& ll : lines_list) {
        ll.text = join_spans_text(ll.spans);
        ll.math_share = 0.0f;
        size_t mc = 0, tc = 0;
        int words = 0;
        bool in_word = false;
        for (const auto& sp : ll.spans) {
            tc += sp.text.size();
            if (sp.is_math) mc += sp.text.size();
        }
        for (char c : ll.text) {
            bool sp2 = (c == ' ');
            if (!sp2 && !in_word) { ++words; in_word = true; }
            else if (sp2) in_word = false;
        }
        ll.word_count = words;
        ll.math_share = tc > 0 ? static_cast<float>(mc) / static_cast<float>(tc) : 0.0f;
    }

    // Mark display equations while we still have per-character geometry, and
    // heal the fragmentation that per-span math detection leaves behind
    // ("10m/s2" must not come out as "$10m $/ $s $^{2").
    for (auto& ll : lines_list) {
        size_t n = ll.spans.size();
        if (n == 0) continue;
        bool any_math = false;
        for (const auto& sp : ll.spans) {
            if (sp.is_math) any_math = true;
        }
        for (size_t i = 1; i < n; ++i) {
            if (ll.spans[i].script != 0 && !ll.spans[i - 1].is_math
                && !ll.spans[i - 1].text.empty() && ll.spans[i - 1].text.size() <= 12) {
                ll.spans[i - 1].is_math = true;
                any_math = true;
            }
        }
        if (any_math) {
            for (size_t i = 1; i + 1 < n; ++i) {
                Span& sp = ll.spans[i];
                if (sp.is_math || sp.text.empty()) continue;
                if (!ll.spans[i - 1].is_math || !ll.spans[i + 1].is_math) continue;
                // Short bare tokens wedged between two math spans (variables,
                // digits, unit letters) belong to the surrounding expression.
                if (sp.text.size() > 4) continue;
                bool alnum = true;
                for (unsigned char ch : sp.text) {
                    if (!std::isalnum(ch) && ch != '.' && ch != '/') { alnum = false; break; }
                }
                if (alnum) sp.is_math = true;
            }
        }
        size_t math_chars = 0, total_chars = 0;
        for (const auto& sp : ll.spans) {
            total_chars += sp.text.size();
            if (sp.is_math) math_chars += sp.text.size();
        }
        float share = total_chars > 0 ? static_cast<float>(math_chars) / static_cast<float>(total_chars) : 0.0f;
        ll.math_share = share;
        ll.is_display_math = false;
        if (any_math && share >= 0.55f && ll.text.find('=') != std::string::npos
            && ll.text.size() <= 160 && ll.word_count <= 30) {
            ll.is_display_math = true;
        }
    }

    pr.lines.clear();
    pr.lines.reserve(lines_list.size());
    for (auto& ll : lines_list) {
        if (ll.spans.empty()) continue;
        float minx = 1e9f, maxx = -1e9f, miny = 1e9f, maxy = -1e9f;
        for (auto& ss : ll.spans) {
            if (ss.bbox.x0 < minx) minx = ss.bbox.x0;
            if (ss.bbox.x1 > maxx) maxx = ss.bbox.x1;
            if (ss.bbox.y0 < miny) miny = ss.bbox.y0;
            if (ss.bbox.y1 > maxy) maxy = ss.bbox.y1;
            if (ss.font_size > 0.01f) fsizes_all.push_back(ss.font_size);
        }
        ll.bbox.x0 = minx; ll.bbox.x1 = maxx; ll.bbox.y0 = miny; ll.bbox.y1 = maxy;
        pr.lines.push_back(std::move(ll));
    }
    PageTextStats st;
    if (!fsizes_all.empty()) {
        std::sort(fsizes_all.begin(), fsizes_all.end());
        st.font_size_min = fsizes_all.front();
        st.font_size_max = fsizes_all.back();
        st.font_size_median = fsizes_all[fsizes_all.size()/2];
        double sumv = 0.0;
        for (auto v : fsizes_all) sumv += v;
        st.font_size_mean = static_cast<float>(sumv / (double)fsizes_all.size());
        st.font_sizes = std::move(fsizes_all);
    }
    st.span_count = 0; st.line_count = pr.lines.size();
    for (auto& ll : pr.lines) st.span_count += ll.spans.size();
    pr.stats = std::move(st);
    fz_drop_stext_page(c, stp);
    if (cidp) fz_drop_stext_page(c, cidp);
}

void Extractor::extract_images_from_page(fz_page* page, PageResult& pr) {
    fz_context* c = ctx_.get();
    fz_stext_page* stp = nullptr;
    bool ok = false;
    fz_try(c) {
        fz_stext_options opts;
        std::memset(&opts, 0, sizeof(opts));
        opts.flags = FZ_STEXT_PRESERVE_IMAGES;
        stp = fz_new_stext_page(c, fz_bound_page(c, page));
        fz_device* dev = fz_new_stext_device(c, stp, &opts);
        fz_run_page(c, page, dev, fz_identity, nullptr);
        fz_drop_device(c, dev);
        ok = true;
    } fz_catch(c) {
        ok = false;
    }
    if (!ok || !stp) {
        if (stp) fz_drop_stext_page(c, stp);
        return;
    }
    for (fz_stext_block* blk = stp->first_block; blk; blk = blk->next) {
        if (blk->type != FZ_STEXT_BLOCK_IMAGE) continue;
        fz_rect ibox = blk->bbox;
        if (fz_is_empty_rect(ibox)) continue;
        if (fz_is_infinite_rect(ibox)) continue;
        float pad = 1.0f;
        fz_rect r;
        r.x0 = ibox.x0 - pad; r.y0 = ibox.y0 - pad;
        r.x1 = ibox.x1 + pad; r.y1 = ibox.y1 + pad;
        float zoom = 150.0f / 72.0f;
        if (config_.image_dpi_hint > 0) {
            zoom = static_cast<float>(config_.image_dpi_hint) / 72.0f;
        }
        if (config_.max_image_dimension > 0) {
            float rw = r.x1 - r.x0;
            float rh = r.y1 - r.y0;
            float maxd = static_cast<float>(config_.max_image_dimension);
            if (rw > 0.0f && rh > 0.0f) {
                float zmax = std::min(maxd / rw, maxd / rh);
                if (zoom > zmax) zoom = zmax;
            }
        }
        if (zoom <= 0.0f) zoom = 1.0f;
        int w = static_cast<int>((r.x1 - r.x0) * zoom + 0.5f);
        int h = static_cast<int>((r.y1 - r.y0) * zoom + 0.5f);
        if (w < 1) w = 1;
        if (h < 1) h = 1;
        fz_pixmap* pm = nullptr;
        fz_try(c) {
            pm = fz_new_pixmap(c, fz_device_rgb(c), w, h, nullptr, 0);
            if (pm) {
                fz_matrix ctm = fz_concat(fz_scale(zoom, zoom),
                                          fz_translate(-r.x0, -r.y0));
                fz_device* dev = fz_new_draw_device_with_bbox(c, ctm, pm, nullptr);
                fz_run_page(c, page, dev, fz_identity, nullptr);
                fz_drop_device(c, dev);
            }
        } fz_catch(c) {
            if (pm) { fz_drop_pixmap(c, pm); pm = nullptr; }
        }
        if (!pm) continue;
        ImageRef ir;
        ir.page_num = pr.page_num;
        ir.index = static_cast<int>(pr.images.size());
        ir.bbox = to_rectf(ibox);
        ir.page_bbox = pr.page_bbox;
        ir.width = static_cast<uint32_t>(pm->w);
        ir.height = static_cast<uint32_t>(pm->h);
        ir.filename = make_image_filename(pr.page_num, ir.index);
        ir.rel_path_md = (fs::path(config_.assets_rel_prefix) / ir.filename).string();
        ir.out_path = (config_.assets_dir / ir.filename).string();
        std::string fnpng = ir.out_path;
        if (config_.image_format == "png" || fnpng.find('.') == std::string::npos) {
            if (fnpng.size() < 4 || fnpng.substr(fnpng.size()-4) != ".png") fnpng += ".png";
        }
        ir.out_path = fnpng;
        ir.rel_path_md = (fs::path(config_.assets_rel_prefix) / fs::path(fnpng).filename()).string();
        bool saved = false;
        fz_try(c) {
            fz_save_pixmap_as_png(c, pm, fnpng.c_str());
            saved = true;
        } fz_catch(c) {
            std::fprintf(stderr, "extract: png save failed (%s): %s\n",
                         fs::path(fnpng).filename().string().c_str(), fz_caught_message(c));
            std::fflush(stderr);
        }
        if (saved) {
            pr.images.push_back(std::move(ir));
        } else {
            std::error_code rm_ec;
            fs::remove(fnpng, rm_ec);
        }
        fz_drop_pixmap(c, pm);
    }
    fz_drop_stext_page(c, stp);
}

std::vector<Block> Extractor::analyze_layout(const std::vector<Line>& lines, PageResult& pr) {
    (void)pr;
    std::vector<Block> blocks;
    if (lines.empty()) return blocks;
    Block cur;
    cur.type = BlockType::Paragraph;
    cur.page_num = pr.page_num;
    for (const auto& l : lines) {
        std::string lt = join_spans_text(l.spans);
        lt = trim_copy(lt);
        if (lt.empty()) continue;
        if (cur.lines.empty()) {
            cur.lines.push_back(l);
            cur.bbox = l.bbox;
        } else {
            cur.lines.push_back(l);
            if (l.bbox.x0 < cur.bbox.x0) cur.bbox.x0 = l.bbox.x0;
            if (l.bbox.x1 > cur.bbox.x1) cur.bbox.x1 = l.bbox.x1;
            if (l.bbox.y0 < cur.bbox.y0) cur.bbox.y0 = l.bbox.y0;
            if (l.bbox.y1 > cur.bbox.y1) cur.bbox.y1 = l.bbox.y1;
        }
    }
    if (!cur.lines.empty()) blocks.push_back(std::move(cur));
    return blocks;
}

void Extractor::detect_columns(PageResult& pr) {
    if (!config_.detect_columns || pr.lines.size() < 4) return;
    std::vector<float> cx;
    cx.reserve(pr.lines.size());
    for (auto& l : pr.lines) cx.push_back(l.bbox.center_x());
    std::sort(cx.begin(), cx.end());
    cx.erase(std::unique(cx.begin(), cx.end(), [this](float a,float b){ return is_near(a,b,4.0f); }), cx.end());
    if (cx.size() >= 2) {
        float gap = cx[1] - cx[0];
        if (gap > config_.column_gap_min) {
            pr.stats.has_two_columns = true;
            pr.stats.column_gap = gap;
            pr.stats.left_col_x1 = cx[0] + gap * 0.4f;
            pr.stats.right_col_x0 = cx[1] - gap * 0.4f;
        }
    }
}

void Extractor::detect_headers_footers(PageResult& pr, std::vector<Block>& blocks) {
    if (!config_.drop_headers_footers || blocks.empty() || pr.page_height <= 0) return;
    float mtop = pr.page_height * config_.header_footer_margin_pct;
    float mbot = pr.page_height * config_.header_footer_margin_pct;
    std::vector<Block> kept;
    kept.clear();
    for (auto& b : blocks) {
        bool hf = false;
        if (b.bbox.y1 <= mtop + 1.0f) hf = true;
        if (b.bbox.y0 >= (pr.page_height - mbot - 1.0f)) hf = true;
        if (!hf) kept.push_back(std::move(b));
    }
    blocks.swap(kept);
}

// Drop text that repeats on many pages. Page furniture such as
// "Ethiofetena.com Ethiopian No 1 Educational Website" sits inside the text
// area rather than in the header/footer band, so the geometric filter cannot see
// it; repetition across pages is the reliable signal. Body text that legitimately
// recurs (a chapter title, a figure number) is spared by requiring a short,
// punctuation-light string that appears on a large share of pages.
void Extractor::drop_repeated_page_text(std::vector<Block>& blocks, size_t page_count) {
    if (blocks.empty() || page_count < 4) return;
    std::map<std::string, std::set<int>> seen;
    for (const auto& b : blocks) {
        for (const auto& l : b.lines) {
            std::string key = to_lower_copy(trim_copy(join_spans_text(l.spans)));
            if (key.size() < 8 || key.size() > 90) continue;
            // Require it to look like furniture, not a sentence.
            int letters = 0, punct = 0;
            for (char c : key) {
                if (std::isalpha(static_cast<unsigned char>(c))) ++letters;
                else if (std::isspace(static_cast<unsigned char>(c))) continue;
                else if (c == '.' || c == ',') continue;
                else ++punct;
            }
            if (letters < 6) continue;
            if (punct > 1) continue;
            seen[key].insert(b.page_num);
        }
    }
    size_t need = std::max<size_t>(4, page_count / 4);
    std::set<std::string> furniture;
    for (const auto& kv : seen) {
        if (kv.second.size() >= need) furniture.insert(kv.first);
    }
    if (furniture.empty()) return;
    size_t removed = 0;
    for (auto& b : blocks) {
        std::vector<Line> kept;
        kept.reserve(b.lines.size());
        for (auto& l : b.lines) {
            std::string key = to_lower_copy(trim_copy(join_spans_text(l.spans)));
            if (furniture.count(key)) { ++removed; continue; }
            kept.push_back(std::move(l));
        }
        b.lines = std::move(kept);
    }
    {
        std::lock_guard<std::mutex> lk(diag_mutex_);
        dropped_repeated_ += removed;
    }
}

void Extractor::classify_blocks(std::vector<Block>& blocks, const PageTextStats& stats, int page_num) {
    float base = stats.font_size_median;
    if (base < 1.0f) base = stats.font_size_mean;
    if (base < 1.0f) base = 10.0f;
    for (auto& b : blocks) {
        b.page_num = page_num;
        if (b.lines.empty()) { b.type = BlockType::Ignore; continue; }
        float fmax = 0.0f;
        for (auto& l : b.lines) for (auto& s : l.spans) if (s.font_size > fmax) fmax = s.font_size;
        b.font_size_max = fmax;
        if (fmax >= base * 1.60f) { b.type = BlockType::Chapter; b.level = 1; }
        else if (fmax >= base * 1.40f) { b.type = BlockType::Section; b.level = 2; }
        else if (fmax >= base * 1.20f) { b.type = BlockType::Subsection; b.level = 3; }
        else if (fmax >= base * 1.12f) { b.type = BlockType::Subsubsection; b.level = 4; }
        else { b.type = BlockType::Paragraph; b.level = 0; }
        std::string bt = block_text(b);
        if (contains_token(bt, config_.chapter_keywords)) {
            if (b.level > 2) b.level = 1;
            b.type = BlockType::Chapter;
        }
        if (contains_token(bt, config_.figure_keywords) && bt.size() < 120) {
            b.type = BlockType::Caption;
        }
    }
}

void Extractor::refine_headings(std::vector<Block>& blocks, const PageTextStats& stats) {
    (void)stats;
    for (auto& b : blocks) {
        if (b.type == BlockType::Paragraph || b.type == BlockType::Unknown) {
            std::string bt = to_lower_copy(block_text(b));
            if (bt.find("chapter") == 0 || bt.find("unit") == 0) {
                b.type = BlockType::Chapter; b.level = 1;
            }
        }
    }
}

void Extractor::detect_lists(std::vector<Block>& blocks) {
    for (auto& b : blocks) {
        if (b.type != BlockType::Paragraph) continue;
        if (b.lines.empty()) continue;
        std::string first = trim_copy(join_spans_text(b.lines.front().spans));
        if (first.size() < 6) {
            if (!first.empty() && (is_bullet_char(first[0]) || std::isdigit(static_cast<unsigned char>(first[0])))) {
                if (std::isdigit(static_cast<unsigned char>(first[0]))) b.type = BlockType::OrderedListItem;
                else b.type = BlockType::ListItem;
            }
        }
    }
}

void Extractor::attach_captions(std::vector<Block>& blocks, std::vector<ImageRef>& images) {
    for (auto& im : images) {
        if (im.has_caption_attached) continue;
        float bestd = 1e9f;
        size_t besti = static_cast<size_t>(-1);
        for (size_t i = 0; i < blocks.size(); ++i) {
            if (blocks[i].type == BlockType::Caption) continue;
            float dy = blocks[i].bbox.y0 - im.bbox.y1;
            if (dy >= -4.0f && dy <= config_.caption_near_dist) {
                float dx = std::fabs(blocks[i].bbox.center_x() - im.bbox.center_x());
                float d = dy*dy + dx*dx;
                if (d < bestd) { bestd = d; besti = i; }
            }
        }
        if (besti < blocks.size()) {
            std::string ct = block_text(blocks[besti]);
            if (contains_token(ct, config_.figure_keywords) || ct.size() < 140) {
                blocks[besti].type = BlockType::Caption;
                blocks[besti].caption_text = ct;
                im.caption_hint = ct;
                im.has_caption_attached = true;
            }
        }
    }
}

std::vector<Block> Extractor::merge_document_blocks(const std::vector<PageResult>& pages) {
    std::vector<Block> all;
    for (const auto& pr : pages) {
        for (const auto& b : pr.blocks) all.push_back(b);
    }
    std::sort(all.begin(), all.end(), [](const Block& a, const Block& b){
        if (std::fabs(a.bbox.y0 - b.bbox.y0) > 4.0f) return a.bbox.y0 < b.bbox.y0;
        return a.bbox.x0 < b.bbox.x0;
    });
    return all;
}

void Extractor::normalize_blocks(std::vector<Block>& blocks) {
    std::vector<Block> out;
    out.clear();
    for (auto& b : blocks) {
        std::string bt = block_text(b);
        bt = trim_copy(bt);
        if (bt.empty()) continue;
        b.id = make_stable_id(b.page_num, static_cast<int>(out.size()), b);
        b.anchor = make_anchor(b, out.size());
        out.push_back(std::move(b));
    }
    blocks.swap(out);
}

void Extractor::build_outline(std::vector<Block>& blocks, DocResult& dr) {
    dr.outline.clear();
    for (const auto& b : blocks) {
        if (b.type == BlockType::Chapter || b.type == BlockType::Section || b.type == BlockType::Subsection || b.type == BlockType::Subsubsection || b.type == BlockType::Title) {
            // A real heading is short. Long blocks that merely had a large font
            // were producing 1900-character table-of-contents entries, one per
            // page, which is what the audit flagged as oversized anchors.
            std::string title = trim_copy(block_text(b));
            if (title.empty()) continue;
            if (title.size() > 120) {
                size_t cut = title.find(". ");
                if (cut != std::string::npos && cut + 2 <= 120) title = title.substr(0, cut + 1);
                else continue;
            }
            DocOutlineItem oi;
            oi.title = title;
            oi.level = b.level;
            if (oi.level == 0 && b.type == BlockType::Title) oi.level = 0;
            oi.page_num = b.page_num;
            oi.y = b.bbox.y0;
            dr.outline.push_back(std::move(oi));
        }
    }
}

void Extractor::render_markdown(const DocResult& dr, const std::filesystem::path& md_path) {
    std::ofstream f(md_path, std::ios::binary);
    if (!f.is_open()) return;
    f << "# " << fs::path(dr.input_path).filename().string() << "\n\n";
    if (!dr.outline.empty()) {
        f << "## Table of Contents\n\n";
        for (const auto& oi : dr.outline) {
            std::string indent;
            int l = oi.level;
            if (l <= 0) l = 1;
            for (int i = 1; i < l; ++i) indent += "  ";
            f << indent << "- [" << escape_md(oi.title) << "](#" << slugify(oi.title) << ")\n";
        }
        f << "\n";
    }
    if (!dr.all_images.empty()) {
        f << "## Figures\n\n";
        for (const auto& im : dr.all_images) {
            f << "- ![" << escape_md(im.caption_hint.empty() ? im.filename : im.caption_hint) << "](" << im.rel_path_md << ")";
            f << " _(Page " << im.page_num << ", " << im.filename << ")_\n";
        }
        f << "\n";
    }
    f << "---\n\n";
    for (const auto& b : dr.merged_blocks) {
        std::string raw = block_text(b);
        if (raw.empty()) continue;
        // Every block, whatever path built it, passes through here. Doing the
        // separation here rather than inside render_line_text also covers the
        // block_text fallbacks, and block_text stays free of "$" delimiters so
        // that TOC titles, slugs and dedup keys are unaffected.
        std::string bt = separate_latex_commands(render_block_text(b));
        if (trim_copy(bt).empty()) continue;
        switch (b.type) {
            case BlockType::Title:
                f << "# " << bt << "\n\n"; break;
            case BlockType::Chapter:
                f << "\n## " << bt << "\n\n"; break;
            case BlockType::Section:
                f << "\n### " << bt << "\n\n"; break;
            case BlockType::Subsection:
                f << "\n#### " << bt << "\n\n"; break;
            case BlockType::Subsubsection:
                f << "\n##### " << bt << "\n\n"; break;
            case BlockType::Caption:
                f << "_Figure: " << bt << "_\n\n"; break;
            case BlockType::ListItem:
                f << "- " << bt << "\n"; break;
            case BlockType::OrderedListItem:
                f << "1. " << bt << "\n"; break;
            case BlockType::Code:
                f << "```text\n" << bt << "\n```\n\n"; break;
            case BlockType::Quote:
                f << "> " << bt << "\n\n"; break;
            default:
                if (!bt.empty() && bt.back() == '\n') f << bt;
                else f << bt << "\n\n";
                break;
        }
    }
    f.close();
}

void Extractor::write_per_chapter_markdown(const DocResult& dr) {
    (void)dr;
}

std::string Extractor::make_anchor(const Block& b, size_t idx) {
    (void)b; (void)idx;
    return slugify(block_text(b));
}

std::string Extractor::make_image_filename(int page_num, int idx) {
    std::ostringstream ss;
    ss << config_.image_prefix << "-p" << std::setfill('0') << std::setw(3) << page_num
       << "-i" << std::setfill('0') << std::setw(2) << idx
       << "." << config_.image_format;
    return ss.str();
}

std::string Extractor::make_stable_id(int page_num, int block_idx, const Block& b) {
    std::ostringstream ss;
    ss << "p" << page_num << "-b" << block_idx << "-" << static_cast<int>(b.type) << "-" << b.level;
    return ss.str();
}

std::string Extractor::escape_md(const std::string& s) {
    return escape_md_plain(s);
}

std::string Extractor::escape_md_plain(const std::string& s) {
    std::string o;
    o.reserve(s.size() + 8);
    for (size_t i = 0; i < s.size(); ++i) {
        char c = s[i];
        // Only characters that are actually special in Markdown get escaped.
        // Parentheses, dashes, dots, '+' and '!' are escaped nowhere: they are
        // only structural at the very start of a line, which is handled by the
        // block emitters, and escaping them destroyed the readability of the
        // output (and collided with LaTeX).
        // Backslash is deliberately NOT escaped: every backslash in the output is
        // one of our own LaTeX commands, and escaping it turned \times into
        // \\times. Markdown gives a backslash no special meaning on its own.
        bool esc = (c == '`' || c == '*' || c == '_' ||
                    c == '[' || c == ']' || c == '<' || c == '>' || c == '|');
        if (!esc && i == 0) {
            esc = (c == '#' || c == '>' );
        }
        if (esc) o.push_back('\\');
        o.push_back(c);
    }
    return o;
}


void Extractor::note_unmapped_glyph(const std::string& name) {
    std::lock_guard<std::mutex> lk(diag_mutex_);
    ++unmapped_glyphs_[name];
}

void Extractor::dump_diagnostics() const {
    std::lock_guard<std::mutex> lk(diag_mutex_);
    std::vector<std::pair<std::string, int>> v(unmapped_glyphs_.begin(), unmapped_glyphs_.end());
    std::sort(v.begin(), v.end(), [](const std::pair<std::string, int>& a,
                                     const std::pair<std::string, int>& b) {
        return a.second > b.second;
    });
    std::cerr << "[SCHOLARET-DIAG] math spans: " << stat_math_spans_ << "\n";
    std::cerr << "[SCHOLARET-DIAG] display math lines: " << stat_display_lines_ << "\n";
    std::cerr << "[SCHOLARET-DIAG] superscript spans: " << stat_sup_spans_ << "\n";
    std::cerr << "[SCHOLARET-DIAG] subscript spans: " << stat_sub_spans_ << "\n";
    std::cerr << "[SCHOLARET-DIAG] glyphs recovered: " << stat_glyph_fixed_ << "\n";
    std::cerr << "[SCHOLARET-DIAG] distinct unmapped glyph names: " << v.size() << "\n";
    std::cerr << "[SCHOLARET-DIAG] prose spans rejected as math: " << prose_rejected_ << "\n";
    std::cerr << "[SCHOLARET-DIAG] repeated footer lines dropped: " << dropped_repeated_ << "\n";
    std::cerr << "[SCHOLARET-DIAG] math rules:";
    for (const auto& kv : math_rules_) {
        std::cerr << " [" << kv.first << "=" << kv.second << "]";
    }
    std::cerr << "\n[SCHOLARET-DIAG] math rule samples:";
    for (const auto& s : math_rule_samples_) {
        std::cerr << "\n  " << s;
    }
    std::cerr << "\n[SCHOLARET-DIAG] top unmapped glyphs:";
    size_t shown = 0;
    for (const auto& p : v) {
        if (shown++ >= 25) break;
        std::cerr << " [" << p.first << "=" << p.second << "]";
    }
    std::cerr << "\n";
}

std::string Extractor::render_line_text(const Line& l) {
    std::string out;
    bool prev_math = false;
    for (const auto& sp : l.spans) {
        if (sp.text.empty()) continue;
        const bool m = sp.is_math;
        if (m != prev_math) {
            // A boundary before a math span needs a separator. Without it
            // "P" + "\u00d7" + "pascal" rendered as one token.
            if (!out.empty() && out.back() != ' ') out.push_back(' ');
            out.push_back('$');
            prev_math = m;
        }
        std::string piece = m ? apply_radicals(sp.text) : escape_md_plain(sp.text);
        if (sp.script == 1) {
            piece = "^{" + piece + "}";
        } else if (sp.script == 2) {
            piece = "_{" + piece + "}";
        }
        out += piece;
    }
    if (prev_math) out.push_back('$');
    return out;
}

std::string Extractor::render_block_text(const Block& b) {
    if (b.type == BlockType::Code) return block_text(b);
    if (b.lines.empty()) return escape_md_plain(block_text(b));

    // A paragraph that is entirely one displayed equation becomes a $$ block;
    // otherwise each qualifying line is lifted out into its own $$ block.
    bool all_display = true;
    for (const auto& l : b.lines) {
        if (!l.is_display_math) { all_display = false; break; }
    }
    if (all_display && b.lines.size() >= 1) {
        std::string out = "$$";
        for (const auto& l : b.lines) {
            out += "\n";
            out += render_line_text(l);
        }
        out += "\n$$\n";
        std::lock_guard<std::mutex> lk(diag_mutex_);
        ++stat_display_lines_;
        return out;
    }

    std::string out;
    for (size_t i = 0; i < b.lines.size(); ++i) {
        const Line& l = b.lines[i];
        if (i) {
            if (l.is_display_math) out += "\n$$\n";
            else out.push_back(' ');
        }
        out += render_line_text(l);
        if (l.is_display_math) {
            out += "\n$$\n";
            std::lock_guard<std::mutex> lk(diag_mutex_);
            ++stat_display_lines_;
        }
    }
    return out;
}

std::string Extractor::clean_text(const std::string& s) {
    return trim_copy(collapse_whitespace(s));
}

std::string Extractor::normalize_whitespace(const std::string& s) {
    return collapse_whitespace(s);
}

std::string Extractor::join_spans_text(const std::vector<Span>& spans) {
    std::string t;
    for (const auto& sp : spans) t += sp.text;
    return t;
}

std::string Extractor::block_text(const Block& b) {
    std::string t;
    for (const auto& l : b.lines) {
        t += join_spans_text(l.spans);
        t += '\n';
    }
    while (!t.empty() && (t.back() == '\n' || t.back() == '\r')) t.pop_back();
    return collapse_whitespace(t);
}

float Extractor::compute_median(std::vector<float> vals) {
    if (vals.empty()) return 0.0f;
    std::sort(vals.begin(), vals.end());
    size_t m = vals.size() / 2;
    return vals[m];
}

float Extractor::compute_mean(const std::vector<float>& vals) {
    if (vals.empty()) return 0.0f;
    double s = 0.0;
    for (auto v : vals) s += v;
    return static_cast<float>(s / (double)vals.size());
}

bool Extractor::is_near(float a, float b, float tol) const noexcept {
    return std::fabs(a - b) <= tol;
}

bool Extractor::rects_overlap_vert(const RectF& a, const RectF& b, float tol) const noexcept {
    return !(a.y1 < b.y0 - tol || b.y1 < a.y0 - tol);
}

bool Extractor::rects_overlap_horiz(const RectF& a, const RectF& b, float tol) const noexcept {
    return !(a.x1 < b.x0 - tol || b.x1 < a.x0 - tol);
}

bool Extractor::is_header_footer_zone(const RectF& bbox, const RectF& page_bbox) const noexcept {
    float h = page_bbox.height();
    if (h <= 0) return false;
    float m = h * 0.12f;
    return (bbox.y1 <= page_bbox.y0 + m + 0.5f || bbox.y0 >= page_bbox.y1 - m - 0.5f);
}

bool Extractor::is_likely_bold(const std::string& font_name, float weight) const noexcept {
    std::string f = to_lower_copy(font_name);
    if (f.find("bold") != std::string::npos || f.find("black") != std::string::npos || f.find("heavy") != std::string::npos) return true;
    if (weight > 12.0f) return true;
    return false;
}

bool Extractor::is_likely_italic(const std::string& font_name) const noexcept {
    std::string f = to_lower_copy(font_name);
    return (f.find("italic") != std::string::npos || f.find("oblique") != std::string::npos);
}

int Extractor::infer_heading_level(float font_size, float size_base, float max_size) const {
    if (max_size < 1.0f) max_size = font_size;
    if (size_base < 1.0f) size_base = font_size;
    float r = font_size / (size_base > 0 ? size_base : 1.0f);
    if (r >= 1.60f) return 1;
    if (r >= 1.40f) return 2;
    if (r >= 1.20f) return 3;
    if (r >= 1.12f) return 4;
    if (font_size >= max_size * 0.98f && font_size >= size_base * 1.08f) return 2;
    return 0;
}

} // namespace scholaret

int main(int argc, char** argv) {
    scholaret::ExtractConfig cfg;
    cfg.output_dir = fs::current_path() / "out";
    cfg.assets_dir = cfg.output_dir / "assets";
    cfg.md_filename = "book.md";
    cfg.output_md = cfg.output_dir / cfg.md_filename;
    cfg.assets_rel_prefix = "assets";
    cfg.image_prefix = "fig";
    cfg.image_format = "png";
    cfg.threads = 0;
    cfg.extract_images = true;
    cfg.extract_text = true;
    cfg.use_outline = true;
    cfg.detect_columns = true;
    cfg.merge_across_pages = true;
    cfg.drop_headers_footers = true;
    cfg.preserve_layout = true;
    cfg.verbose = false;
    cfg.quiet = false;
    cfg.column_gap_min = 24.0f;
    cfg.heading_keywords = {"chapter", "unit", "part", "section", "ቀንድ", "ምዕራፍ", "ክፍል"};
    cfg.figure_keywords = {"fig", "figure", "fig.", "figure.", "ምስል", "ስዕል"};
    cfg.chapter_keywords = {"chapter", "ምዕራፍ", "unit"};
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--in" && i+1 < argc) { cfg.input_path = argv[++i]; }
        else if (a == "--out" && i+1 < argc) { cfg.output_dir = argv[++i]; cfg.assets_dir = cfg.output_dir / "assets"; cfg.output_md = cfg.output_dir / cfg.md_filename; }
        else if (a == "--md" && i+1 < argc) { cfg.md_filename = argv[++i]; cfg.output_md = cfg.output_dir / cfg.md_filename; }
        else if (a == "--assets" && i+1 < argc) { cfg.assets_dir = cfg.output_dir / argv[++i]; cfg.assets_rel_prefix = argv[i]; }
        else if (a == "--batch" && i+1 < argc) { cfg.batch_list = argv[++i]; cfg.batch_mode = true; }
        else if (a == "--threads" && i+1 < argc) { cfg.threads = std::stoi(argv[++i]); }
        else if (a == "--no-images") { cfg.extract_images = false; }
        else if (a == "--no-cols") { cfg.detect_columns = false; }
        else if (a == "--no-merge") { cfg.merge_across_pages = false; }
        else if (a == "--no-hf") { cfg.drop_headers_footers = false; }
        else if (a == "--imgfmt" && i+1 < argc) { cfg.image_format = argv[++i]; }
        else if (a == "--imgpref" && i+1 < argc) { cfg.image_prefix = argv[++i]; }
        else if (a == "-v" || a == "--verbose") { cfg.verbose = true; }
        else if (a == "-q" || a == "--quiet") { cfg.quiet = true; }
        else if (a == "-h" || a == "--help") {
            std::cout << "ScholarET PDF Extractor\n";
            std::cout << "Usage: extract --in file.pdf [--out out] [--threads 0] [--batch list.txt] [--no-images]\n";
            return 0;
        }
    }
    try {
        fs::create_directories(cfg.output_dir);
        fs::create_directories(cfg.assets_dir);
    } catch (...) {}
    scholaret::Extractor ex(cfg);
    if (cfg.batch_mode) {
        auto rs = ex.extract_batch();
        if (!cfg.quiet) std::cout << "Processed " << rs.size() << " docs\n";
        return rs.empty() ? 1 : 0;
    }
    if (cfg.input_path.empty()) {
        if (!cfg.quiet) std::cerr << "Missing --in <pdf>\n";
        return 1;
    }
    auto dr = ex.extract_single(cfg.input_path);
    if (!cfg.quiet) {
        std::cout << "Input: " << dr.input_path << "\n";
        std::cout << "Pages: " << dr.total_pages << "\n";
        std::cout << "Blocks: " << dr.total_blocks << "\n";
        std::cout << "Images: " << dr.total_images << "\n";
        std::cout << "MD: " << dr.output_md_path << "\n";
        std::cout << "Assets: " << dr.assets_dir << "\n";
        std::cout << "Time(ms): " << dr.elapsed_ms.count() << "\n";
        std::cout << "OK: " << (dr.success ? "yes" : "no") << "\n";
    }
    return dr.success ? 0 : 1;
}
