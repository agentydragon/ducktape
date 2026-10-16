/* Replicate, step by step, the pseudoterminal sequence every Nix build runs
 * unconditionally in DerivationBuilderImpl::startBuilder() / openSlave()
 * (src/libstore/unix/build/derivation-builder.cc, Nix 2.31.3), and report
 * errno for each step separately.
 *
 * The failure reported in nix-community/nix-on-droid#495 is
 *   "getting pseudoterminal attributes: Permission denied"
 * which is the tcgetattr() below -- i.e. ioctl(slave, TCGETS) returning
 * EACCES *after* the open() of the slave has already succeeded. The point of
 * splitting every step out is to find which one actually fails on a given
 * device, since several of them can produce EACCES.
 *
 * Build statically so it runs on Android without bionic:
 *   gcc -static -O0 -o ptytest ptytest.c
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

static void step(const char *name, int failed)
{
    if (failed)
        printf("  %-34s FAIL errno=%d (%s)\n", name, errno, strerror(errno));
    else
        printf("  %-34s ok\n", name);
    fflush(stdout);
}

int main(void)
{
    printf("== ptytest: uid=%d euid=%d gid=%d\n", getuid(), geteuid(), getgid());

    /* Parent side, exactly as startBuilder() does it. */
    int master = posix_openpt(O_RDWR | O_NOCTTY);
    step("posix_openpt(O_RDWR|O_NOCTTY)", master < 0);
    if (master < 0)
        return 1;

    char *slave = ptsname(master);
    step("ptsname(master)", slave == NULL);
    if (!slave)
        return 1;
    printf("  slave path = %s\n", slave);

    struct stat st;
    if (stat(slave, &st) == 0)
        printf("  slave stat  = mode=0%o uid=%d gid=%d\n",
               st.st_mode & 07777, st.st_uid, st.st_gid);
    else
        printf("  slave stat  = FAIL errno=%d (%s)\n", errno, strerror(errno));

    /* Nix only takes this branch when it has a build user (a nixbld uid);
     * nix-on-droid has none, so this is here purely to show whether the
     * SELinux "setattr" denial that policy predicts is real. */
    step("chmod(slave,0600) [buildUser path]", chmod(slave, 0600) != 0);
    step("chown(slave,uid,0) [buildUser path]", chown(slave, getuid(), 0) != 0);

    /* grantpt() is compiled in only on __APPLE__ in Nix 2.31.3, but glibc's
     * version is what older Nix used on Linux, so measure it too. */
    step("grantpt(master)", grantpt(master) != 0);
    step("unlockpt(master)", unlockpt(master) != 0);

    /* Child side: openSlave(). This is what throws the reported error. */
    pid_t pid = fork();
    if (pid == 0) {
        printf("-- child pid=%d (openSlave)\n", getpid());
        char *sname = ptsname(master);
        step("child ptsname(master)", sname == NULL);
        int sfd = open(sname, O_RDWR | O_NOCTTY);
        step("child open(slave,O_RDWR|O_NOCTTY)", sfd < 0);
        if (sfd < 0)
            _exit(2);

        struct termios term;
        int rc = tcgetattr(sfd, &term); /* ioctl(TCGETS) */
        step("child tcgetattr  <-- issue #495", rc != 0);
        if (rc != 0)
            _exit(3);

        cfmakeraw(&term);
        step("child tcsetattr(TCSANOW)", tcsetattr(sfd, TCSANOW, &term) != 0);
        step("child dup2(slave,2)", dup2(sfd, STDERR_FILENO) < 0);
        _exit(0);
    }

    int status = 0;
    waitpid(pid, &status, 0);
    printf("== child exit status=%d\n", WEXITSTATUS(status));
    printf("== RESULT: %s\n", WEXITSTATUS(status) == 0 ? "PTY SEQUENCE OK" : "PTY SEQUENCE FAILED");
    return 0;
}
