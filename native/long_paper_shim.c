/*
 * Long-paper support shim for Brother's brscan5 SANE backend.
 *
 * The DS-640 firmware reports long-paper support up to 1828.8 mm (72 in),
 * but the brscan5 SANE frontend exposes the normal 355.6 mm (14 in) limit
 * through its exported rangeYmm option constraint.  This shim widens that
 * constraint after sane_open(), without modifying Brother's binary.
 *
 * Build:
 *   cc -shared -fPIC -O2 -Wall -Wextra -o libds640_longpaper.so \
 *      native/long_paper_shim.c -ldl
 *
 * Use:
 *   LD_PRELOAD=/path/to/libds640_longpaper.so scanimage ...
 *
 * Environment:
 *   DS640_LONG_PAPER_MAX_MM     Maximum scan height in mm (default 1828.8)
 *   DS640_LONG_PAPER_FORCE      Set to 1 to force a long-paper scan type
 *   DS640_LONG_PAPER_TYPE       23 = narrow, 24 = full width (default 24)
 *   DS640_LONG_PAPER_WIDTH_MM   Forced scan width in mm (per-type default)
 *   DS640_LONG_PAPER_HEIGHT_MM  Forced scan height in mm (default 1828.8)
 *   DS640_LONG_PAPER_DEBUG      Set to 1 to print constraint details to stderr
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef int (*sane_open_fn)(const char *, void **);
typedef int (*start_scanning_fn)(void *);

static sane_open_fn next_sane_open;
static start_scanning_fn next_start_scanning;
static int debug_enabled;

static int env_flag(const char *name) {
    const char *value = getenv(name);
    return value != NULL && value[0] != '\0' && strcmp(value, "0") != 0;
}

static int env_int(const char *name, int fallback) {
    const char *value = getenv(name);
    char *end = NULL;
    long parsed;

    if (value == NULL || value[0] == '\0') {
        return fallback;
    }
    parsed = strtol(value, &end, 10);
    if (end == NULL || end == value || *end != '\0') {
        return fallback;
    }
    return (int)parsed;
}

static double env_double(const char *name, double fallback) {
    const char *value = getenv(name);
    char *end = NULL;
    double parsed;

    if (value == NULL || value[0] == '\0') {
        return fallback;
    }
    parsed = strtod(value, &end);
    if (end == NULL || end == value || *end != '\0') {
        return fallback;
    }
    return parsed;
}

/*
 * libsane dlopens the backend without RTLD_GLOBAL, so the RTLD_DEFAULT
 * lookup cannot see rangeYmm.  Ask the dynamic loader for the already
 * loaded object instead.
 */
static void *find_backend(void) {
    static const char *const names[] = {
        "libsane-brother5.so.1",
        "libsane-brother5.so.1.0.7",
        "libsane-brother5.so",
    };
    size_t index;

    for (index = 0; index < sizeof(names) / sizeof(names[0]); index++) {
        void *handle = dlopen(names[index], RTLD_LAZY | RTLD_NOLOAD);
        if (handle != NULL) {
            return handle;
        }
    }
    return NULL;
}

__attribute__((constructor))
static void long_paper_init(void) {
    debug_enabled = env_flag("DS640_LONG_PAPER_DEBUG");
}

static int read_i32(const unsigned char *base, size_t offset) {
    int value;
    memcpy(&value, base + offset, sizeof(value));
    return value;
}

static unsigned int read_u8(const unsigned char *base, size_t offset) {
    return base[offset];
}

static void dump_scan_parameters(const char *when, const void *parameter) {
    const unsigned char *pointer = (const unsigned char *)parameter;

    fprintf(stderr,
            "ds640-longpaper: %s res=%dx%d color=%d papertype=%d "
            "custom=(%d,%d) areaSelected=%u "
            "select=(off=%d,%d size=%d,%d unit=%d) source=%d\n",
            when,
            read_i32(pointer, 0x00),
            read_i32(pointer, 0x04),
            read_i32(pointer, 0x08),
            read_i32(pointer, 0x0c),
            read_i32(pointer, 0x10),
            read_i32(pointer, 0x14),
            read_u8(pointer, 0x1c),
            read_i32(pointer, 0x20),
            read_i32(pointer, 0x24),
            read_i32(pointer, 0x28),
            read_i32(pointer, 0x2c),
            read_i32(pointer, 0x30),
            read_i32(pointer, 0x40));
}

static void write_i32(unsigned char *base, size_t offset, int value) {
    memcpy(base + offset, &value, sizeof(value));
}

static void write_u8(unsigned char *base, size_t offset, unsigned char value) {
    base[offset] = value;
}

/*
 * Force the driver into one of its long-paper modes.  The paper type table
 * in libLxBsScanCoreApi gives:
 *   23 = long paper, narrow width (107.95 mm)
 *   24 = long paper, wide width   (215.9 mm)
 * Both use the requested scan-area height as the maximum length.
 * The scan-area unit is 1 = 0.01 mm.
 */
static void force_long_paper(void *parameter) {
    unsigned char *pointer = (unsigned char *)parameter;
    int paper_type = env_int("DS640_LONG_PAPER_TYPE", 24);
    double default_width_mm = paper_type == 24 ? 215.9 : 107.95;
    double width_mm = env_double("DS640_LONG_PAPER_WIDTH_MM", default_width_mm);
    double height_mm = env_double("DS640_LONG_PAPER_HEIGHT_MM", 1828.8);
    int width = (int)(width_mm * 100.0 + 0.5);
    int height = (int)(height_mm * 100.0 + 0.5);

    write_i32(pointer, 0x0c, paper_type);
    write_i32(pointer, 0x10, width);
    write_i32(pointer, 0x14, height);
    write_u8(pointer, 0x1c, 1);
    write_i32(pointer, 0x20, 0);
    write_i32(pointer, 0x24, 0);
    write_i32(pointer, 0x28, width);
    write_i32(pointer, 0x2c, height);
    write_i32(pointer, 0x30, 1);
}

int bssdkwrapper_StartScanning(void *parameter) {
    int status;

    if (next_start_scanning == NULL) {
        void *backend = find_backend();
        next_start_scanning = backend != NULL
            ? (start_scanning_fn)dlsym(backend, "bssdkwrapper_StartScanning")
            : NULL;
        if (next_start_scanning == NULL) {
            if (debug_enabled) {
                fprintf(stderr, "ds640-longpaper: could not resolve bssdkwrapper_StartScanning\n");
            }
            return 2; /* SANE_STATUS_CANCELLED */
        }
    }

    if (env_flag("DS640_LONG_PAPER_FORCE")) {
        force_long_paper(parameter);
        if (debug_enabled) {
            dump_scan_parameters("StartScanning forced", parameter);
        }
    } else if (debug_enabled) {
        dump_scan_parameters("StartScanning before", parameter);
    }
    status = next_start_scanning(parameter);
    if (debug_enabled) {
        fprintf(stderr, "ds640-longpaper: StartScanning status=%d\n", status);
    }
    return status;
}

int sane_open(const char *device_name, void **handle) {
    int status;

    if (next_sane_open == NULL) {
        next_sane_open = (sane_open_fn)dlsym(RTLD_NEXT, "sane_open");
        if (next_sane_open == NULL) {
            if (debug_enabled) {
                fprintf(stderr, "ds640-longpaper: could not resolve sane_open\n");
            }
            return 1; /* SANE_STATUS_UNSUPPORTED */
        }
        if (debug_enabled) {
            Dl_info info;
            if (dladdr((void *)next_sane_open, &info) != 0) {
                fprintf(stderr, "ds640-longpaper: next sane_open=%p in %s\n",
                        (void *)next_sane_open, info.dli_fname);
            }
        }
    }

    if (debug_enabled) {
        fprintf(stderr, "ds640-longpaper: sane_open(%s) -> calling real\n", device_name);
    }
    status = next_sane_open(device_name, handle);
    if (debug_enabled) {
        fprintf(stderr, "ds640-longpaper: real sane_open status=%d handle=%p\n",
                status, handle != NULL ? *handle : NULL);
    }
    if (status == 0) {
        void *backend = find_backend();
        int *range = backend != NULL ? (int *)dlsym(backend, "rangeYmm") : NULL;

        if (range == NULL) {
            if (debug_enabled) {
                fprintf(stderr, "ds640-longpaper: could not find rangeYmm\n");
            }
        } else {
            double max_mm = env_double("DS640_LONG_PAPER_MAX_MM", 1828.8);
            int new_max = (int)(max_mm * 65536.0 + 0.5);

            if (debug_enabled) {
                fprintf(stderr,
                        "ds640-longpaper: rangeYmm before min=%d max=%d quantum=%d\n",
                        range[0], range[1], range[2]);
            }
            if (new_max > range[1]) {
                range[1] = new_max;
            }
            if (debug_enabled) {
                fprintf(stderr,
                        "ds640-longpaper: rangeYmm after  min=%d max=%d quantum=%d\n",
                        range[0], range[1], range[2]);
            }
        }
    }
    return status;
}
