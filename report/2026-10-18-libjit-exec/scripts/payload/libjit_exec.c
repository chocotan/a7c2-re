/* Camera-side one-shot command runner for ILCE-7CM2 (A7C II) firmware 2.01.
 *
 * Entry: the Jiritsu service itself dlopens the fixed path
 * /tmp/jiritsu/libJit.so after a successful file write to it. Writing this
 * object to that path therefore runs _init() inside the service process, with
 * the service's credentials (euid 0 on this firmware).
 *
 * Design constraints, each of which is a deliberate limit:
 *   - no libc, no NEEDED libraries, syscalls only: the hosting process has
 *     already mapped its own world; we must not depend on anything.
 *   - no /etc, no preload, no DEBUG command: nothing is left armed. The object
 *     unlinks itself before reading its request, so even a crash cannot leave
 *     a module that will be loaded again.
 *   - the shell starts as a child (clone without CLONE_VM) and _init returns
 *     immediately, so the USB request completes even if the command is slow.
 *     A blocking _init would stall the service for the command's whole runtime.
 *   - the command is a shell script from /tmp/jiritsu/a7c2-exec.request, so the
 *     host owns the wrapper and this object stays a generic runner.
 *
 * Result files (all under /tmp/jiritsu, which is /dev/nflasha6, 25 MiB):
 *   a7c2-exec.start   this object's own record: pid, euid, status code
 *   a7c2-exec.out     child's stdout and stderr
 *   a7c2-exec.status  completion line, written by the shell script's last line
 */
typedef unsigned long u64;
typedef long i64;
typedef unsigned int u32;
typedef unsigned char u8;

static i64 sc(i64 no, i64 a, i64 b, i64 c, i64 d, i64 e, i64 f) {
    register i64 x0 __asm__("x0") = a;
    register i64 x1 __asm__("x1") = b;
    register i64 x2 __asm__("x2") = c;
    register i64 x3 __asm__("x3") = d;
    register i64 x4 __asm__("x4") = e;
    register i64 x5 __asm__("x5") = f;
    register i64 x8 __asm__("x8") = no;
    __asm__ volatile("svc 0" : "+r"(x0)
                     : "r"(x1), "r"(x2), "r"(x3), "r"(x4), "r"(x5), "r"(x8)
                     : "memory", "cc");
    return x0;
}

/* aarch64 generic syscall numbers used here. */
#define SYS_CLOSE     57
#define SYS_OPENAT    56
#define SYS_READ      63
#define SYS_WRITE     64
#define SYS_UNLINKAT  35
#define SYS_DUP3      24
#define SYS_CHDIR     49
#define SYS_GETEUID  175
#define SYS_GETPID   172
#define SYS_CLONE    220
#define SYS_EXECVE   221
#define SYS_EXIT_GRP  94

#define AT_FDCWD     (-100)
#define O_RDONLY     0
#define O_WRONLY     1
#define O_CREAT      64
#define O_TRUNC      512
#define SIGCHLD      17

#define REQUEST_MAX  2048
#define START_MAX     256

static const char SELF_PATH[]    = "/tmp/jiritsu/libJit.so";
static const char REQUEST_PATH[] = "/tmp/jiritsu/a7c2-exec.request";
static const char START_PATH[]   = "/tmp/jiritsu/a7c2-exec.start";
static const char OUTPUT_PATH[]  = "/tmp/jiritsu/a7c2-exec.out";
/* Written by the shell script's last line, not by this object; known here only
 * so cleanup mode and the previous-run sweep can remove it. */
static const char STATUS_PATH[]  = "/tmp/jiritsu/a7c2-exec.status";
static const char SHELL_PATH[]   = "/bin/sh";
static const char TAG[]          = "A7JITE01";
/* A request that starts with this marker makes this object wipe every file the
 * tool ever leaves in the work area and return without starting a shell. The
 * host proves it worked by the absence of those paths, so no record is written
 * (writing one would defeat the purpose). */
static const char CLEANUP[]      = "#A7JIT-CLEANUP";

static char request[REQUEST_MAX];
static char record[START_MAX];

static u32 append(char *out, u32 at, const char *s) {
    while (*s && at < START_MAX - 1) out[at++] = *s++;
    return at;
}

/* Signed decimal without libc; only used for pid/euid/status fields. */
static u32 append_int(char *out, u32 at, i64 value) {
    char digits[24];
    u32 n = 0;
    u64 magnitude;
    if (value < 0) {
        if (at < START_MAX - 1) out[at++] = '-';
        magnitude = (u64)(-value);
    } else {
        magnitude = (u64)value;
    }
    do {
        digits[n++] = (char)('0' + (int)(magnitude % 10));
        magnitude /= 10;
    } while (magnitude);
    while (n && at < START_MAX - 1) out[at++] = digits[--n];
    return at;
}

static int starts_with(const char *text, const char *prefix) {
    while (*prefix) {
        if (*text++ != *prefix++) return 0;
    }
    return 1;
}

static void discard(const char *path) {
    sc(SYS_UNLINKAT, AT_FDCWD, (i64)path, 0, 0, 0, 0);
}

static void write_start_record(i64 status) {
    u32 at = 0;
    at = append(record, at, TAG);
    at = append(record, at, " v1 pid=");
    at = append_int(record, at, sc(SYS_GETPID, 0, 0, 0, 0, 0, 0));
    at = append(record, at, " euid=");
    at = append_int(record, at, sc(SYS_GETEUID, 0, 0, 0, 0, 0, 0));
    at = append(record, at, " status=");
    at = append_int(record, at, status);
    at = append(record, at, "\n");
    i64 fd = sc(SYS_OPENAT, AT_FDCWD, (i64)START_PATH,
                O_WRONLY | O_CREAT | O_TRUNC, 0644, 0, 0);
    if (fd < 0) return;
    sc(SYS_WRITE, fd, (i64)record, at, 0, 0, 0);
    sc(SYS_CLOSE, fd, 0, 0, 0, 0, 0);
}

/* Read the request file into `request`, NUL-terminating it. Returns its length
 * or a negative errno. A missing trailing NUL is tolerated: the host may write
 * a bare script, and we own the terminator. */
static i64 read_request(void) {
    i64 fd = sc(SYS_OPENAT, AT_FDCWD, (i64)REQUEST_PATH, O_RDONLY, 0, 0, 0);
    if (fd < 0) return fd;
    u32 done = 0;
    while (done < REQUEST_MAX - 1) {
        i64 n = sc(SYS_READ, fd, (i64)(request + done), REQUEST_MAX - 1 - done, 0, 0, 0);
        if (n < 0) {
            sc(SYS_CLOSE, fd, 0, 0, 0, 0, 0);
            return n;
        }
        if (!n) break;
        done += (u32)n;
    }
    sc(SYS_CLOSE, fd, 0, 0, 0, 0, 0);
    request[done] = '\0';
    return (i64)done;
}

static void run_child(i64 output_fd) {
    /* Only async-signal-safe raw syscalls from here: the child of a
     * multithreaded process may not touch locks its parent held. */
    sc(SYS_DUP3, output_fd, 1, 0, 0, 0, 0);
    sc(SYS_DUP3, output_fd, 2, 0, 0, 0, 0);
    if (output_fd > 2) sc(SYS_CLOSE, output_fd, 0, 0, 0, 0, 0);
    sc(SYS_CHDIR, (i64)"/", 0, 0, 0, 0, 0);
    char *argv[4];
    argv[0] = (char *)"sh";
    argv[1] = (char *)"-c";
    argv[2] = request;
    argv[3] = 0;
    char *envp[3];
    envp[0] = (char *)"PATH=/bin:/sbin:/usr/bin:/usr/sbin";
    envp[1] = (char *)"HOME=/";
    envp[2] = 0;
    sc(SYS_EXECVE, (i64)SHELL_PATH, (i64)argv, (i64)envp, 0, 0, 0);
    /* execve only returns on failure; say so where the host looks for output. */
    const char failure[] = "A7JITE01 exec-failed\n";
    sc(SYS_WRITE, 1, (i64)failure, sizeof(failure) - 1, 0, 0, 0);
    sc(SYS_EXIT_GRP, 127, 0, 0, 0, 0, 0);
    for (;;) {}
}

__attribute__((visibility("default"))) void _init(void) {
    /* Disarm first: after this syscall nothing on disk can reload us, no
     * matter how the rest of this function fails. */
    sc(SYS_UNLINKAT, AT_FDCWD, (i64)SELF_PATH, 0, 0, 0, 0);

    if (sc(SYS_GETEUID, 0, 0, 0, 0, 0, 0) != 0) {
        write_start_record(-13 /* EACCES */);
        return;
    }
    i64 length = read_request();
    if (length <= 0) {
        write_start_record(length ? length : -22 /* EINVAL */);
        return;
    }
    if (starts_with(request, CLEANUP)) {
        discard(REQUEST_PATH);
        discard(START_PATH);
        discard(STATUS_PATH);
        discard(OUTPUT_PATH);
        return;
    }
    /* Drop the previous run's records here rather than relying on the host to
     * truncate them: this object is already root, and residue should not
     * accumulate on a 25 MiB partition across sessions. */
    discard(START_PATH);
    discard(STATUS_PATH);
    discard(OUTPUT_PATH);
    i64 output_fd = sc(SYS_OPENAT, AT_FDCWD, (i64)OUTPUT_PATH,
                       O_WRONLY | O_CREAT | O_TRUNC, 0644, 0, 0);
    if (output_fd < 0) {
        write_start_record(output_fd);
        return;
    }
    /* clone(SIGCHLD, 0, ...) is aarch64's fork: the child gets a copy, not a
     * shared address space, so execve cannot disturb the hosting service. */
    i64 child = sc(SYS_CLONE, SIGCHLD, 0, 0, 0, 0, 0);
    if (child == 0) run_child(output_fd);
    if (output_fd > 2) sc(SYS_CLOSE, output_fd, 0, 0, 0, 0, 0);
    write_start_record(child < 0 ? child : 0);
    /* Return to dlopen. We never wait for the child: the daemon owns reaping,
     * and blocking here would hold the USB request for the command's runtime. */
}
