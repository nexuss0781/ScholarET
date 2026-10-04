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
#include <cstring>
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
    ctx_.reset(c, [](fz_context* p) { if (p) fz_drop_context(p); });
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
    fz_stext_page* stp = nullptr;
    fz_try(c) {
        stp = fz_new_stext_page(c, fz_bound_page(c, page));
        fz_device* dev = fz_new_stext_device(c, stp, nullptr);
        fz_run_page(c, page, dev, fz_identity, nullptr);
        fz_drop_device(c, dev);
    } fz_catch(c) {
        if (stp) fz_drop_stext_page(c, stp);
        return;
    }
    std::vector<Line> lines_list;
    std::vector<float> fsizes_all;
    fz_stext_block* blk = stp->first_block;
    while (blk) {
        if (blk->type == FZ_STEXT_BLOCK_TEXT) {
            fz_stext_line* ln = blk->u.text.first_line;
            while (ln) {
                Line l;
                l.bbox = to_rectf(ln->bbox);
                l.baseline_y = ln->baseline.y;
                l.line_height = ln->height;
                fz_stext_span* sp = ln->first_span;
                while (sp) {
                    if (sp->text && sp->len > 0) {
                        Span s;
                        s.text.assign(sp->text, static_cast<size_t>(sp->len));
                        if (!s.text.empty() && static_cast<unsigned char>(s.text.back()) == '\n') s.text.pop_back();
                        if (!s.text.empty() && static_cast<unsigned char>(s.text.back()) == '\r') s.text.pop_back();
                        s.bbox = to_rectf(sp->bbox);
                        if (sp->font) {
                            const char* fn = fz_font_name(c, sp->font);
                            if (fn) s.font_name_raw = fn;
                            s.font_family = s.font_name_raw;
                        }
                        s.font_size = sp->size;
                        s.font_weight = 0.0f;
                        std::string fnl = to_lower_copy(s.font_name_raw);
                        s.is_bold = (fnl.find("bold") != std::string::npos);
                        s.is_italic = (fnl.find("italic") != std::string::npos || fnl.find("oblique") != std::string::npos);
                        s.is_monospace = (fnl.find("mono") != std::string::npos || fnl.find("courier") != std::string::npos || fnl.find("typewriter") != std::string::npos);
                        s.is_serif = (fnl.find("serif") != std::string::npos);
                        s.is_sans = (fnl.find("sans") != std::string::npos);
                        if (s.is_bold) s.font_weight *= 1.05f;
                        if (!s.text.empty() && s.font_size > 0.01f) {
                            lines_list.push_back(l);
                            lines_list.back().spans.clear();
                            lines_list.back().spans.push_back(std::move(s));
                            l = lines_list.back();
                        }
                    }
                    sp = sp->next;
                }
                if (!l.spans.empty()) {
                    lines_list.push_back(std::move(l));
                }
                ln = ln->next;
            }
        }
        blk = blk->next;
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
}

void Extractor::extract_images_from_page(fz_page* page, PageResult& pr) {
    fz_context* c = ctx_.get();
    fz_image* img_list = nullptr;
    int nimg = 0;
    fz_try(c) {
        nimg = fz_page_count_images(c, page);
    } fz_catch(c) { nimg = 0; }
    for (int i = 0; i < nimg; ++i) {
        fz_image* img = nullptr;
        fz_rect ibox = fz_empty_rect;
        fz_try(c) {
            img = fz_page_get_image(c, page, i, &ibox);
        } fz_catch(c) { img = nullptr; }
        if (!img) continue;
        fz_pixmap* pm = nullptr;
        fz_try(c) {
            pm = fz_get_pixmap_from_image(c, img, nullptr, nullptr, nullptr, 0);
        } fz_catch(c) {
            fz_drop_image(c, img);
            continue;
        }
        if (!pm) {
            fz_drop_image(c, img);
            continue;
        }
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
        fz_try(c) {
            fz_save_pixmap_as_png(c, pm, fnpng.c_str());
        } fz_catch(c) {}
        pr.images.push_back(std::move(ir));
        fz_drop_pixmap(c, pm);
        fz_drop_image(c, img);
    }
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
            DocOutlineItem oi;
            oi.title = block_text(b);
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
        std::string bt = block_text(b);
        if (bt.empty()) continue;
        switch (b.type) {
            case BlockType::Title:
                f << "# " << escape_md(bt) << "\n\n"; break;
            case BlockType::Chapter:
                f << "\n## " << escape_md(bt) << "\n\n"; break;
            case BlockType::Section:
                f << "\n### " << escape_md(bt) << "\n\n"; break;
            case BlockType::Subsection:
                f << "\n#### " << escape_md(bt) << "\n\n"; break;
            case BlockType::Subsubsection:
                f << "\n##### " << escape_md(bt) << "\n\n"; break;
            case BlockType::Caption:
                f << "_Figure: " << escape_md(bt) << "_\n\n"; break;
            case BlockType::ListItem:
                f << "- " << escape_md(bt) << "\n"; break;
            case BlockType::OrderedListItem:
                f << "1. " << escape_md(bt) << "\n"; break;
            case BlockType::Code:
                f << "```text\n" << bt << "\n```\n\n"; break;
            case BlockType::Quote:
                f << "> " << escape_md(bt) << "\n\n"; break;
            default:
                f << escape_md(bt) << "\n\n"; break;
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
    std::string o;
    o.reserve(s.size() * 2);
    for (char c : s) {
        if (c == '\\' || c == '`' || c == '*' || c == '_' || c == '{' || c == '}' || c == '[' || c == ']' || c == '(' || c == ')' || c == '#' || c == '+' || c == '-' || c == '.' || c == '!' || c == '|') {
            o.push_back('\\');
        }
        o.push_back(c);
    }
    return o;
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
