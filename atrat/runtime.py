"""The whole Atrat runtime: a small header emitted at the top of every C file."""

RUNTIME = r"""
/* ------------------------------------------------------------------
 * Atrat runtime -- ~360 lines, no GC, no external dependencies.
 * ------------------------------------------------------------------ */
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <math.h>
#include <errno.h>
#include <time.h>

typedef struct { const char* ptr; size_t len; } AtrStr;
typedef struct { char* ptr; size_t len; size_t cap; } AtrString;
typedef struct { char _unused; } AtrUnit;

/* Panic cleanup registry. Generated code registers every live owning local
 * and function parameter; moves and normal scope exits unregister it. On a
 * panic the runtime destroys all remaining owners in reverse registration
 * order before terminating. This is cleanup, not unwinding: control never
 * returns to Atrat code. */
typedef void (*AtrCleanupFn)(void*);
typedef struct {
    void* ptr;
    AtrCleanupFn fn;
} AtrCleanupEntry;

static AtrCleanupEntry* atr_cleanups = NULL;
static size_t atr_cleanup_len = 0;
static size_t atr_cleanup_cap = 0;
static int atr_panicking = 0;

static inline void atr_panic(const char* msg);

static inline void atr_cleanup_register(void* ptr, AtrCleanupFn fn) {
    if (atr_cleanup_len == atr_cleanup_cap) {
        size_t ncap = atr_cleanup_cap ? atr_cleanup_cap * 2 : 16;
        AtrCleanupEntry* grown = (AtrCleanupEntry*)realloc(
            atr_cleanups, ncap * sizeof(AtrCleanupEntry));
        if (!grown) {
            atr_panic("out of memory while registering cleanup");
        }
        atr_cleanups = grown;
        atr_cleanup_cap = ncap;
    }
    atr_cleanups[atr_cleanup_len].ptr = ptr;
    atr_cleanups[atr_cleanup_len].fn = fn;
    atr_cleanup_len++;
}

static inline void atr_cleanup_unregister(void* ptr) {
    for (size_t i = atr_cleanup_len; i > 0; i--) {
        if (atr_cleanups[i - 1].ptr == ptr) {
            atr_cleanups[i - 1] = atr_cleanups[atr_cleanup_len - 1];
            atr_cleanup_len--;
            return;
        }
    }
}

static inline void atr_cleanup_dispose(void) {
    free(atr_cleanups);
    atr_cleanups = NULL;
    atr_cleanup_len = 0;
    atr_cleanup_cap = 0;
}

static inline void atr_cleanup_run(void) {
    while (atr_cleanup_len > 0) {
        AtrCleanupEntry e = atr_cleanups[--atr_cleanup_len];
        e.fn(e.ptr);
    }
    atr_cleanup_dispose();
}

static inline void atr_panic_str(AtrStr msg) {
    if (atr_panicking) {
        fputs("atrat panic: panic while cleaning up\n", stderr);
        exit(101);
    }
    atr_panicking = 1;
    fputs("atrat panic: ", stderr);
    if (msg.ptr) fwrite(msg.ptr, 1, msg.len, stderr);
    fputc('\n', stderr);
    atr_cleanup_run();
    fflush(NULL);
    exit(101);
}

static inline void atr_panic(const char* msg) {
    AtrStr s;
    s.ptr = msg;
    s.len = msg ? strlen(msg) : 0;
    atr_panic_str(s);
}

static inline void* atr_alloc(size_t n) {
    void* p = calloc(1, n ? n : 1);
    if (!p) atr_panic("out of memory");
    return p;
}

static inline void* atr_realloc(void* p, size_t n) {
    void* q = realloc(p, n ? n : 1);
    if (!q) atr_panic("out of memory");
    return q;
}

static inline AtrStr atr_str_lit(const char* p, size_t n) {
    AtrStr s; s.ptr = p; s.len = n; return s;
}

static inline int atr_str_eq(AtrStr a, AtrStr b) {
    if (a.len != b.len) return 0;
    if (a.len == 0) return 1;
    return memcmp(a.ptr, b.ptr, a.len) == 0;
}

static inline int atr_str_cmp(AtrStr a, AtrStr b) {
    size_t n = a.len < b.len ? a.len : b.len;
    int c = n ? memcmp(a.ptr, b.ptr, n) : 0;
    if (c != 0) return c;
    if (a.len < b.len) return -1;
    if (a.len > b.len) return 1;
    return 0;
}

static inline AtrString atr_string_new(void) {
    AtrString s; s.ptr = NULL; s.len = 0; s.cap = 0; return s;
}

static inline void atr_string_reserve(AtrString* s, size_t extra) {
    if (extra > SIZE_MAX - s->len - 1) atr_panic("string too large");
    size_t need = s->len + extra + 1;
    if (need <= s->cap) return;
    size_t cap = s->cap ? s->cap : 16;
    while (cap < need) cap *= 2;
    s->ptr = (char*)atr_realloc(s->ptr, cap);
    s->cap = cap;
}

static inline void atr_string_push_bytes(AtrString* s, const char* p, size_t n) {
    if (!n) return;
    if (n > SIZE_MAX - s->len - 1) atr_panic("string too large");
    atr_string_reserve(s, n);
    memcpy(s->ptr + s->len, p, n);
    s->len += n;
    s->ptr[s->len] = '\0';
}

static inline void atr_string_push_cstr(AtrString* s, const char* p) {
    if (p) atr_string_push_bytes(s, p, strlen(p));
}

static inline void atr_string_push_char(AtrString* s, uint32_t c) {
    char buf[4];
    size_t n = 0;
    if (c < 0x80) { buf[n++] = (char)c; }
    else if (c < 0x800) { buf[n++] = (char)(0xC0 | (c >> 6)); buf[n++] = (char)(0x80 | (c & 0x3F)); }
    else if (c < 0x10000) { buf[n++] = (char)(0xE0 | (c >> 12)); buf[n++] = (char)(0x80 | ((c >> 6) & 0x3F)); buf[n++] = (char)(0x80 | (c & 0x3F)); }
    else { buf[n++] = (char)(0xF0 | (c >> 18)); buf[n++] = (char)(0x80 | ((c >> 12) & 0x3F)); buf[n++] = (char)(0x80 | ((c >> 6) & 0x3F)); buf[n++] = (char)(0x80 | (c & 0x3F)); }
    atr_string_push_bytes(s, buf, n);
}

static inline void atr_string_push_i64(AtrString* s, int64_t v) {
    char buf[32];
    snprintf(buf, sizeof buf, "%lld", (long long)v);
    atr_string_push_cstr(s, buf);
}

static inline void atr_string_push_u64(AtrString* s, uint64_t v) {
    char buf[32];
    snprintf(buf, sizeof buf, "%llu", (unsigned long long)v);
    atr_string_push_cstr(s, buf);
}

static inline void atr_string_push_f64(AtrString* s, double v) {
    char buf[64];
    snprintf(buf, sizeof buf, "%.15g", v);
    atr_string_push_cstr(s, buf);
}

static inline void atr_string_push_bool(AtrString* s, bool v) {
    atr_string_push_cstr(s, v ? "true" : "false");
}

static inline void atr_string_free(AtrString* s) {
    free(s->ptr);
    s->ptr = NULL; s->len = 0; s->cap = 0;
}

static inline AtrStr atr_string_view(const AtrString* s) {
    AtrStr r;
    r.ptr = s->ptr ? s->ptr : "";
    r.len = s->len;
    return r;
}

static inline AtrString atr_string_from_view(AtrStr a) {
    AtrString s = atr_string_new();
    atr_string_push_bytes(&s, a.ptr, a.len);
    return s;
}

static inline AtrString atr_string_concat(AtrStr a, AtrStr b) {
    AtrString s = atr_string_new();
    atr_string_reserve(&s, a.len + b.len);
    atr_string_push_bytes(&s, a.ptr, a.len);
    atr_string_push_bytes(&s, b.ptr, b.len);
    return s;
}

static inline void atr_print_str(AtrStr s) {
    if (s.len) fwrite(s.ptr, 1, s.len, stdout);
}

static inline void atr_println_str(AtrStr s) {
    atr_print_str(s);
    fputc('\n', stdout);
}

/* ---- checked integer division, modulo and shifts ---- */
#define ATR_DEF_UINT(T, N) \
static inline T atr_div_##N(T a, T b) { \
    if (b == 0) atr_panic("integer division by zero"); \
    return a / b; \
} \
static inline T atr_mod_##N(T a, T b) { \
    if (b == 0) atr_panic("integer modulo by zero"); \
    return a % b; \
} \
static inline T atr_shl_##N(T a, T b) { \
    if ((unsigned long long)b >= (unsigned long long)(sizeof(T) * 8)) \
        atr_panic("shift amount out of range"); \
    return (T)((unsigned long long)a << b); \
} \
static inline T atr_shr_##N(T a, T b) { \
    if ((unsigned long long)b >= (unsigned long long)(sizeof(T) * 8)) \
        atr_panic("shift amount out of range"); \
    return (T)(a >> b); \
}

#define ATR_DEF_SINT(T, N, MINV) \
static inline T atr_div_##N(T a, T b) { \
    if (b == 0) atr_panic("integer division by zero"); \
    if (b == (T)-1 && a == (T)(MINV)) atr_panic("integer overflow"); \
    return a / b; \
} \
static inline T atr_mod_##N(T a, T b) { \
    if (b == 0) atr_panic("integer modulo by zero"); \
    if (b == (T)-1) return 0; \
    return a % b; \
} \
static inline T atr_shl_##N(T a, T b) { \
    if ((unsigned long long)b >= (unsigned long long)(sizeof(T) * 8)) \
        atr_panic("shift amount out of range"); \
    return (T)((unsigned long long)a << b); \
} \
static inline T atr_shr_##N(T a, T b) { \
    if ((unsigned long long)b >= (unsigned long long)(sizeof(T) * 8)) \
        atr_panic("shift amount out of range"); \
    return (T)(a >> b); \
}

ATR_DEF_SINT(int8_t, i8, INT8_MIN)
ATR_DEF_SINT(int16_t, i16, INT16_MIN)
ATR_DEF_SINT(int32_t, i32, INT32_MIN)
ATR_DEF_SINT(int64_t, i64, INT64_MIN)
ATR_DEF_SINT(intptr_t, isize, INTPTR_MIN)
ATR_DEF_UINT(uint8_t, u8)
ATR_DEF_UINT(uint16_t, u16)
ATR_DEF_UINT(uint32_t, u32)
ATR_DEF_UINT(uint64_t, u64)
ATR_DEF_UINT(size_t, usize)

/* ---- host services (files, environment, arguments, time) ---- */
static int atr_argc = 0;
static char** atr_argv = NULL;

static inline void atr_push_err(AtrString* err, const char* what) {
    if (err) {
        atr_string_push_cstr(err, what ? what : "error");
    }
}

static inline int atr_read_file(AtrStr path, AtrString* out, AtrString* err) {
    char* p = (char*)atr_alloc(path.len + 1);
    if (path.len) memcpy(p, path.ptr, path.len);
    p[path.len] = '\0';
    FILE* f = fopen(p, "rb");
    free(p);
    if (!f) { atr_push_err(err, strerror(errno)); return 0; }
    char buf[8192];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, f)) > 0) {
        atr_string_push_bytes(out, buf, n);
    }
    int bad = ferror(f);
    fclose(f);
    if (bad) { atr_push_err(err, "read error"); return 0; }
    return 1;
}

static inline int atr_write_file(AtrStr path, AtrStr data, AtrString* err) {
    char* p = (char*)atr_alloc(path.len + 1);
    if (path.len) memcpy(p, path.ptr, path.len);
    p[path.len] = '\0';
    FILE* f = fopen(p, "wb");
    free(p);
    if (!f) { atr_push_err(err, strerror(errno)); return 0; }
    if (data.len) fwrite(data.ptr, 1, data.len, f);
    int bad = ferror(f);
    fclose(f);
    if (bad) { atr_push_err(err, "write error"); return 0; }
    return 1;
}

static inline int atr_env(AtrStr name, AtrString* out) {
    char* p = (char*)atr_alloc(name.len + 1);
    if (name.len) memcpy(p, name.ptr, name.len);
    p[name.len] = '\0';
    const char* v = getenv(p);
    free(p);
    if (!v) return 0;
    atr_string_push_cstr(out, v);
    return 1;
}

static inline size_t atr_args_count(void) {
    return atr_argc > 1 ? (size_t)(atr_argc - 1) : 0;
}

static inline int atr_arg_at(size_t i, AtrString* out) {
    if (!atr_argv || i + 1 >= (size_t)atr_argc) return 0;
    atr_string_push_cstr(out, atr_argv[i + 1]);
    return 1;
}

static inline uint64_t atr_now_ms(void) {
    struct timespec ts;
    if (timespec_get(&ts, TIME_UTC) == TIME_UTC) {
        return (uint64_t)ts.tv_sec * 1000ull + (uint64_t)(ts.tv_nsec / 1000000);
    }
    return (uint64_t)time(NULL) * 1000ull;
}

static inline int64_t atr_ipow_i64(int64_t base, int64_t exp) {
    if (exp < 0) atr_panic("negative exponent for integer power");
    int64_t r = 1;
    while (exp > 0) {
        if (exp & 1) r *= base;
        base *= base;
        exp >>= 1;
    }
    return r;
}

"""
