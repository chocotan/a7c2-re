/* Select the firmware's existing file backend, only in an explicit standalone test. */
extern void *dlvsym(void *, const char *, const char *);
extern char *getenv(const char *);
extern int printf(const char *, ...);
extern int fflush(void *);
extern void _exit(int);
static unsigned calls;

unsigned file_media_configured(void) { return calls; }

void mcmn_param_init(void) {
    void *(*lookup)(void *, const char *) = dlvsym((void *)-1, "dlsym", "GLIBC_2.17");
    void (*original)(void) = lookup ? lookup((void *)-1, "mcmn_param_init") : 0;
    unsigned (*get)(int) = lookup ? lookup((void *)-1, "mcmn_param_getul") : 0;
    void (*set)(int, unsigned) = lookup ? lookup((void *)-1, "mcmn_param_setul") : 0;
    if (!original || original == mcmn_param_init || !get || !set) {
        printf("FILE_MEDIA_CONFIG_RESOLVE_FAILED\n"); fflush(0); _exit(126);
    }
    original();
    const char *enabled = getenv("A7C2_FILE_MEDIA_TEST");
    if (enabled && enabled[0] == '1' && !enabled[1]) {
        unsigned before = get(0x69);
        set(0x69, 0);
        calls++;
        printf("FILE_MEDIA_CONFIG call=%u EXISTMEDIADRIVER=%u->%u OMIT_WRITE=%u\n",
               calls, before, get(0x69), get(0xf9));
        fflush(0);
    }
}
