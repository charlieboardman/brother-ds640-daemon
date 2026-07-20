#define _GNU_SOURCE

#include <ctype.h>
#include <dlfcn.h>
#include <errno.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef struct libusb_device_handle libusb_device_handle;

typedef int (*libusb_bulk_transfer_fn)(
    libusb_device_handle *dev_handle,
    unsigned char endpoint,
    unsigned char *data,
    int length,
    int *actual_length,
    unsigned int timeout
);

static libusb_bulk_transfer_fn real_libusb_bulk_transfer;

static bool env_enabled(const char *name) {
    const char *value = getenv(name);
    if (value == NULL || value[0] == '\0') {
        return false;
    }
    return strcmp(value, "0") != 0
        && strcasecmp(value, "false") != 0
        && strcasecmp(value, "no") != 0;
}

static const char *env_default(const char *name, const char *fallback) {
    const char *value = getenv(name);
    return (value != NULL && value[0] != '\0') ? value : fallback;
}

static FILE *open_log(void) {
    const char *path = getenv("DS640_USB_TAP_LOG");
    if (path == NULL || path[0] == '\0') {
        return stderr;
    }
    FILE *handle = fopen(path, "a");
    return handle != NULL ? handle : stderr;
}

static void close_log(FILE *handle) {
    if (handle != NULL && handle != stderr) {
        fclose(handle);
    }
}

static void tap_log(const char *format, ...) {
    FILE *handle = open_log();
    if (handle == NULL) {
        return;
    }

    time_t now = time(NULL);
    struct tm local_tm;
    char timestamp[64] = {0};
    if (localtime_r(&now, &local_tm) != NULL) {
        strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%S%z", &local_tm);
    } else {
        snprintf(timestamp, sizeof(timestamp), "time-unavailable");
    }

    fprintf(handle, "[%s] ", timestamp);
    va_list args;
    va_start(args, format);
    vfprintf(handle, format, args);
    va_end(args);
    fputc('\n', handle);
    fflush(handle);
    close_log(handle);
}

static void log_hex_ascii(const char *label, const unsigned char *data, int length) {
    if (data == NULL || length <= 0) {
        tap_log("%s len=%d", label, length);
        return;
    }

    FILE *handle = open_log();
    if (handle == NULL) {
        return;
    }

    fprintf(handle, "%s len=%d\n", label, length);
    fprintf(handle, "hex=");
    for (int i = 0; i < length; i++) {
        fprintf(handle, "%s%02x", i == 0 ? "" : " ", data[i]);
    }
    fprintf(handle, "\ntext=");
    for (int i = 0; i < length; i++) {
        unsigned char c = data[i];
        if (c == '\n') {
            fputs("\\n", handle);
        } else if (c == '\r') {
            fputs("\\r", handle);
        } else if (c == 0x1b) {
            fputs("\\x1b", handle);
        } else if (c == 0x80) {
            fputs("\\x80", handle);
        } else if (isprint(c)) {
            fputc(c, handle);
        } else {
            fprintf(handle, "\\x%02x", c);
        }
    }
    fputc('\n', handle);
    fflush(handle);
    close_log(handle);
}

static bool starts_with_ssp(const unsigned char *data, int length) {
    static const unsigned char prefix[] = {0x1b, 'S', 'S', 'P', '\n'};
    return data != NULL
        && length >= (int)sizeof(prefix)
        && memcmp(data, prefix, sizeof(prefix)) == 0;
}

static bool starts_with_brother_command(const unsigned char *data, int length) {
    return data != NULL && length >= 4 && data[0] == 0x1b;
}

static bool has_key_line(const unsigned char *cursor, int remaining, const char *key) {
    size_t key_len = strlen(key);
    return remaining >= (int)key_len && memcmp(cursor, key, key_len) == 0;
}

static unsigned char *patch_ssp_auto_continuous(
    const unsigned char *data,
    int length,
    int *patched_length
) {
    *patched_length = length;

    if (!starts_with_ssp(data, length)) {
        return NULL;
    }

    const char *atto = env_default("DS640_USB_TAP_ATTO", "600");
    const char *atfd = env_default("DS640_USB_TAP_ATFD", "30");
    int replacement_len = snprintf(
        NULL,
        0,
        "ATCN=ON\nATTO=%s\nATFD=%s\n",
        atto,
        atfd
    );
    if (replacement_len <= 0) {
        return NULL;
    }

    int capacity = length + replacement_len + 64;
    unsigned char *patched = malloc((size_t)capacity);
    if (patched == NULL) {
        tap_log("patch allocation failed: errno=%d", errno);
        return NULL;
    }

    int out = 0;
    bool inserted = false;
    for (int i = 0; i < length; ) {
        if (
            has_key_line(data + i, length - i, "ATCN=")
            || has_key_line(data + i, length - i, "ATTO=")
            || has_key_line(data + i, length - i, "ATFD=")
        ) {
            if (!inserted) {
                out += snprintf(
                    (char *)patched + out,
                    (size_t)(capacity - out),
                    "ATCN=ON\nATTO=%s\nATFD=%s\n",
                    atto,
                    atfd
                );
                inserted = true;
            }
            while (i < length && data[i] != '\n' && data[i] != 0x80) {
                i++;
            }
            if (i < length && data[i] == '\n') {
                i++;
            }
            continue;
        }

        if (data[i] == 0x80) {
            if (!inserted) {
                out += snprintf(
                    (char *)patched + out,
                    (size_t)(capacity - out),
                    "ATCN=ON\nATTO=%s\nATFD=%s\n",
                    atto,
                    atfd
                );
                inserted = true;
            }
            patched[out++] = data[i++];
            continue;
        }

        patched[out++] = data[i++];
        if (out >= capacity - replacement_len - 2) {
            free(patched);
            tap_log("patch capacity exhausted");
            return NULL;
        }
    }

    *patched_length = out;
    return patched;
}

int libusb_bulk_transfer(
    libusb_device_handle *dev_handle,
    unsigned char endpoint,
    unsigned char *data,
    int length,
    int *actual_length,
    unsigned int timeout
) {
    if (real_libusb_bulk_transfer == NULL) {
        real_libusb_bulk_transfer =
            (libusb_bulk_transfer_fn)dlsym(RTLD_NEXT, "libusb_bulk_transfer");
        if (real_libusb_bulk_transfer == NULL) {
            tap_log("dlsym(libusb_bulk_transfer) failed: %s", dlerror());
            return -99;
        }
    }

    bool is_out = (endpoint & 0x80) == 0;
    bool log_all = env_enabled("DS640_USB_TAP_LOG_ALL");
    bool log_commands = env_enabled("DS640_USB_TAP_LOG_COMMANDS") || log_all;
    bool patch_atcn = env_enabled("DS640_USB_TAP_PATCH_ATCN");

    unsigned char *send_data = data;
    int send_length = length;
    unsigned char *patched = NULL;

    if (is_out && starts_with_brother_command(data, length) && log_commands) {
        log_hex_ascii("bulk-out original", data, length);
    }

    if (is_out && patch_atcn && starts_with_ssp(data, length)) {
        patched = patch_ssp_auto_continuous(data, length, &send_length);
        if (patched != NULL) {
            send_data = patched;
            if (log_commands) {
                log_hex_ascii("bulk-out patched", send_data, send_length);
            }
        } else {
            tap_log("SSP patch requested but no patch was applied");
        }
    }

    int rc = real_libusb_bulk_transfer(
        dev_handle,
        endpoint,
        send_data,
        send_length,
        actual_length,
        timeout
    );

    if (patched != NULL) {
        free(patched);
        if (rc == 0 && actual_length != NULL) {
            *actual_length = length;
        }
    }

    if (log_all) {
        tap_log(
            "bulk-transfer endpoint=0x%02x dir=%s len=%d rc=%d actual=%d timeout=%u",
            endpoint,
            is_out ? "out" : "in",
            send_length,
            rc,
            actual_length != NULL ? *actual_length : -1,
            timeout
        );
    }

    return rc;
}
