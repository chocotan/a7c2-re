/* One open inode: concurrent atomic snapshot replacement cannot mix frames. */
#include "headless-osd.h"
extern int open(const char *, int, ...), close(int);
extern long read(int, void *, unsigned long), write(int, const void *, unsigned long);
extern void _exit(int);
extern int *__errno_location(void);
static int output(const void *data, unsigned long n) {
    const unsigned char *p = data;
    while (n) {
        long written = write(1, p, n);
        if (written < 0 && *__errno_location() == 4) continue;
        if (written <= 0 || (unsigned long)written > n) return -1;
        p += written; n -= written;
    }
    return 0;
}
int hosd_export(const char *path) {
    int fd = open(path, 0);
    if (fd < 0) return 1;
    static const char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    unsigned char raw[768]; char encoded[1025];
    unsigned long total = 0;
    if (output("\nHEADLESS_OSD_BEGIN\n", 20)) { close(fd); return 1; }
    for (;;) {
        long n = read(fd, raw, sizeof raw);
        if (n < 0 && *__errno_location() == 4) continue;
        if (!n) break;
        if (n < 0 || total + n > HOSD_BYTES) { close(fd); return 1; }
        total += n;
        unsigned int k = 0;
        for (long i = 0; i < n; i += 3) {
            unsigned int v = (unsigned int)raw[i] << 16;
            if (i + 1 < n) v |= (unsigned int)raw[i + 1] << 8;
            if (i + 2 < n) v |= raw[i + 2];
            encoded[k++] = alphabet[v >> 18]; encoded[k++] = alphabet[(v >> 12) & 63];
            encoded[k++] = i + 1 < n ? alphabet[(v >> 6) & 63] : '=';
            encoded[k++] = i + 2 < n ? alphabet[v & 63] : '=';
        }
        encoded[k++] = '\n';
        if (output(encoded, k)) { close(fd); return 1; }
    }
    if (close(fd) || total != HOSD_BYTES) return 1;
    return output("HEADLESS_OSD_END\n", 17) ? 1 : 0;
}
#ifndef HOSD_EXPORT_TEST
void _start(void) { _exit(hosd_export(HOSD_PATH)); }
#endif
