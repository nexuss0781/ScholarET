#pragma once

#include <string>
#include <vector>
#include <unordered_map>
#include <unordered_set>
#include <memory>
#include <mutex>
#include <optional>
#include <cstdint>
#include <functional>
#include <filesystem>
#include <array>
#include <map>
#include <set>
#include <variant>
#include <limits>
#include <cmath>
#include <chrono>
#include <algorithm>
#include <utility>
#include <tuple>
#include <cassert>
#include <cstring>
#include <cctype>
#include <atomic>
#include <thread>
#include <future>
#include <queue>
#include <condition_variable>
#include <type_traits>
#include <fstream>
#include <sstream>
#include <iomanip>
#include <regex>
#include <random>
#include <initializer_list>
#include <memory_resource>
#include <span>

struct fz_context;
struct fz_document;
struct fz_page;
struct fz_image;
struct fz_pixmap;
struct fz_font;

namespace scholaret {

struct RectF {
    float x0 = 0.0f;
    float y0 = 0.0f;
    float x1 = 0.0f;
    float y1 = 0.0f;
    
    inline float width() const noexcept { return x1 > x0 ? (x1 - x0) : 0.0f; }
    inline float height() const noexcept { return y1 > y0 ? (y1 - y0) : 0.0f; }
    inline float area() const noexcept { return width() * height(); }
    inline float center_x() const noexcept { return x0 + width() * 0.5f; }
    inline float center_y() const noexcept { return y0 + height() * 0.5f; }
    inline bool empty() const noexcept { return width() <= 0.0f || height() <= 0.0f; }
};

struct Span {
    std::string text;
    std::string font_family;
    std::string font_name_raw;
    float font_size = 0.0f;
    float font_weight = 0.0f;
    bool is_bold = false;
    bool is_italic = false;
    bool is_monospace = false;
    bool is_serif = false;
    bool is_sans = false;
    RectF bbox;
    int script = 0;
    bool is_math = false;
};

struct Line {
    std::vector<Span> spans;
    RectF bbox;
    float baseline_y = 0.0f;
    float line_height = 0.0f;
    bool is_indented = false;
    bool is_centered = false;
    bool is_right_aligned = false;
    float indent_left = 0.0f;
    bool is_display_math = false;
    float math_share = 0.0f;
    int word_count = 0;
    std::string text;
};

enum class BlockType {
    Unknown = 0,
    Title,
    Chapter,
    Section,
    Subsection,
    Subsubsection,
    Paragraph,
    ListItem,
    OrderedListItem,
    Definition,
    Caption,
    Figure,
    Table,
    TableRow,
    Code,
    Quote,
    Footnote,
    Header,
    Footer,
    PageNumber,
    Ignore
};

struct Block {
    BlockType type = BlockType::Paragraph;
    int level = 0;
    std::vector<Line> lines;
    RectF bbox;
    std::string id;
    std::string anchor;
    std::string number;
    std::string label;
    std::string caption_text;
    int page_num = 0;
    int order_index = 0;
    bool is_continued = false;
    bool is_break_after = false;
    float font_size_max = 0.0f;
    float font_weight_max = 0.0f;
};

struct ImageRef {
    int page_num = 0;
    int index = 0;
    std::string out_path;
    std::string rel_path_md;
    std::string filename;
    RectF bbox;
    RectF page_bbox;
    std::string caption_hint;
    std::string alt_text;
    bool has_caption_attached = false;
    uint32_t width = 0;
    uint32_t height = 0;
    int image_type = 0;
};

struct PageTextStats {
    float font_size_min = std::numeric_limits<float>::max();
    float font_size_max = std::numeric_limits<float>::lowest();
    float font_size_mean = 0.0f;
    float font_size_median = 0.0f;
    std::vector<float> font_sizes;
    size_t span_count = 0;
    size_t line_count = 0;
    size_t block_count = 0;
    float content_top = std::numeric_limits<float>::max();
    float content_bottom = std::numeric_limits<float>::lowest();
    float content_left = std::numeric_limits<float>::max();
    float content_right = std::numeric_limits<float>::lowest();
    bool has_two_columns = false;
    float column_gap = 0.0f;
    float left_col_x1 = 0.0f;
    float right_col_x0 = 0.0f;
};

struct PageResult {
    int page_num = 0;
    std::vector<Line> lines;
    std::vector<Block> blocks;
    std::vector<ImageRef> images;
    PageTextStats stats;
    RectF page_bbox;
    float page_width = 0.0f;
    float page_height = 0.0f;
    bool processed = false;
    std::string error;
};

struct DocOutlineItem {
    std::string title;
    int level = 0;
    int page_num = 0;
    std::optional<float> y;
};

struct DocResult {
    std::string input_path;
    std::string output_md_path;
    std::string assets_dir;
    std::vector<PageResult> pages;
    std::vector<ImageRef> all_images;
    std::vector<Block> merged_blocks;
    std::vector<DocOutlineItem> outline;
    size_t total_pages = 0;
    size_t total_blocks = 0;
    size_t total_images = 0;
    bool success = false;
    std::vector<std::string> errors;
    std::chrono::milliseconds elapsed_ms{0};
};

struct ExtractConfig {
    std::filesystem::path input_path;
    std::filesystem::path output_dir;
    std::filesystem::path output_md;
    std::filesystem::path assets_dir;
    std::filesystem::path batch_list;
    std::string assets_rel_prefix = "assets";
    std::string md_filename = "book.md";
    std::string image_prefix = "fig";
    std::string image_format = "png";
    int threads = 0;
    int max_threads = 0;
    bool batch_mode = false;
    bool extract_images = true;
    bool extract_text = true;
    bool use_outline = true;
    bool detect_columns = true;
    bool merge_across_pages = true;
    bool drop_headers_footers = true;
    bool preserve_layout = true;
    bool write_per_chapter = false;
    bool force_gpu_aware = false;
    bool verbose = false;
    bool quiet = false;
    float column_gap_min = 24.0f;
    float heading_size_ratio = 1.10f;
    float para_gap_ratio = 0.85f;
    float min_block_height = 1.0f;
    float min_span_chars = 1;
    float header_footer_margin_pct = 0.10f;
    float caption_near_dist = 60.0f;
    float list_indent_tol = 6.0f;
    float same_line_tol = 2.5f;
    float block_merge_vert_tol = 4.0f;
    float hyphen_join_tol = 2.0f;
    unsigned int image_dpi_hint = 0;
    size_t max_image_dimension = 0;
    std::vector<std::string> ignore_patterns;
    std::vector<std::string> heading_keywords;
    std::vector<std::string> figure_keywords;
    std::vector<std::string> chapter_keywords;
};

class ThreadPool {
public:
    explicit ThreadPool(int threads = 0);
    ~ThreadPool();
    ThreadPool(const ThreadPool&) = delete;
    ThreadPool& operator=(const ThreadPool&) = delete;
    ThreadPool(ThreadPool&&) = delete;
    ThreadPool& operator=(ThreadPool&&) = delete;

    template<class F, class... Args>
    auto enqueue(F&& f, Args&&... args) 
        -> std::future<std::invoke_result_t<F, Args...>>
    {
        using return_type = std::invoke_result_t<F, Args...>;
        auto task = std::make_shared<std::packaged_task<return_type()>>(
            std::bind(std::forward<F>(f), std::forward<Args>(args)...)
        );
        std::future<return_type> res = task->get_future();
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (stop_) {
                throw std::runtime_error("ThreadPool is stopped");
            }
            tasks_.emplace([task]() { (*task)(); });
        }
        cv_.notify_one();
        return res;
    }

    void stop();
    void wait_all();
    size_t size() const noexcept;
    bool empty() const noexcept;
    int thread_count() const noexcept { return static_cast<int>(workers_.size()); }

private:
    std::vector<std::thread> workers_;
    std::queue<std::function<void()>> tasks_;
    mutable std::mutex mutex_;
    std::condition_variable cv_;
    std::atomic<bool> stop_{false};
    std::atomic<size_t> active_{0};
};

class Extractor {
public:
    explicit Extractor(const ExtractConfig& config);
    ~Extractor();

    Extractor(const Extractor&) = delete;
    Extractor& operator=(const Extractor&) = delete;

    DocResult extract_single(const std::filesystem::path& input_path);
    std::vector<DocResult> extract_batch();

private:
    ExtractConfig config_;
    std::unique_ptr<fz_context, std::function<void(fz_context*)>> ctx_;
    std::mutex io_mutex_;
    std::mutex ctx_mutex_;

    bool initialize_context();
    void cleanup_context();
    
    DocResult process_document(const std::filesystem::path& input_path);
    PageResult process_page(fz_document* doc, int page_num, fz_page* page);
    
    void extract_text_from_page(fz_page* page, PageResult& pr);
    void extract_images_from_page(fz_page* page, PageResult& pr);
    
    std::vector<Block> analyze_layout(const std::vector<Line>& lines, PageResult& pr);
    void detect_columns(PageResult& pr);
    void detect_headers_footers(PageResult& pr, std::vector<Block>& blocks);
    void classify_blocks(std::vector<Block>& blocks, const PageTextStats& stats, int page_num);
    void refine_headings(std::vector<Block>& blocks, const PageTextStats& stats);
    void detect_lists(std::vector<Block>& blocks);
    void attach_captions(std::vector<Block>& blocks, std::vector<ImageRef>& images);
    
    std::vector<Block> merge_document_blocks(const std::vector<PageResult>& pages);
    void normalize_blocks(std::vector<Block>& blocks);
    void build_outline(std::vector<Block>& blocks, DocResult& dr);
    
    void render_markdown(const DocResult& dr, const std::filesystem::path& md_path);
    void write_per_chapter_markdown(const DocResult& dr);
    
    std::string make_anchor(const Block& b, size_t idx);
    std::string make_image_filename(int page_num, int idx);
    std::string make_stable_id(int page_num, int block_idx, const Block& b);
    std::string escape_md(const std::string& s);
    std::string escape_md_plain(const std::string& s);
    std::string render_block_text(const Block& b);
    std::string render_line_text(const Line& l);
    std::string glyph_to_latex(fz_context* c, fz_font* font, int gid, int ucs);
    void note_unmapped_glyph(const std::string& name);
    void dump_diagnostics() const;
    std::string clean_text(const std::string& s);
    std::string normalize_whitespace(const std::string& s);
    std::string join_spans_text(const std::vector<Span>& spans);
    std::string block_text(const Block& b);
    
    float compute_median(std::vector<float> vals);
    float compute_mean(const std::vector<float>& vals);
    bool is_near(float a, float b, float tol) const noexcept;
    bool rects_overlap_vert(const RectF& a, const RectF& b, float tol) const noexcept;
    bool rects_overlap_horiz(const RectF& a, const RectF& b, float tol) const noexcept;
    bool is_header_footer_zone(const RectF& bbox, const RectF& page_bbox) const noexcept;
    bool is_likely_bold(const std::string& font_name, float weight) const noexcept;
    bool is_likely_italic(const std::string& font_name) const noexcept;
    int infer_heading_level(float font_size, float size_base, float max_size) const;
    
    void ensure_directories();
    std::vector<std::string> read_batch_list(const std::filesystem::path& list_path);

    mutable std::mutex diag_mutex_;
    std::map<std::string, int> unmapped_glyphs_;
    std::map<std::string, int> math_rules_;
    std::vector<std::string> math_rule_samples_;
    size_t stat_sup_spans_ = 0;
    size_t stat_sub_spans_ = 0;
    size_t stat_math_spans_ = 0;
    size_t stat_display_lines_ = 0;
    size_t stat_glyph_fixed_ = 0;
};

} // namespace scholaret
