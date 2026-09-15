#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile sig_atomic_t stop_signal;

static void request_stop(int signal_number) {
  stop_signal = signal_number;
}

static int install_handler(int signal_number) {
  struct sigaction action = {
      .sa_handler = request_stop,
  };
  sigemptyset(&action.sa_mask);
  return sigaction(signal_number, &action, NULL);
}

static int signal_group(pid_t leader, int signal_number, const char *description) {
  if (kill(-leader, signal_number) == -1 && errno != ESRCH) {
    perror(description);
    return -1;
  }
  return 0;
}

int main(int argc, char *argv[]) {
  if (argc < 4 || strcmp(argv[1], "--native-pid-fd") != 0) {
    fputs("harness supervisor requires --native-pid-fd FD and a command\n", stderr);
    return 64;
  }
  char *end = NULL;
  long descriptor = strtol(argv[2], &end, 10);
  if (*argv[2] == '\0' || *end != '\0' || descriptor < 0) {
    fputs("harness supervisor received an invalid native pid descriptor\n", stderr);
    return 64;
  }
  int native_pid_fd = (int)descriptor;
  if (install_handler(SIGTERM) == -1 || install_handler(SIGUSR1) == -1 || install_handler(SIGALRM) == -1) {
    perror("install harness supervisor signal handler");
    return 125;
  }

  pid_t parent = getppid();
  if (prctl(PR_SET_PDEATHSIG, SIGUSR1) == -1) {
    perror("set harness parent-death signal");
    return 125;
  }
  if (getppid() != parent) {
    stop_signal = SIGUSR1;
  }
  if (stop_signal != 0) {
    return 128 + stop_signal;
  }

  pid_t child = fork();
  if (child == -1) {
    perror("fork harness");
    return 125;
  }
  if (child == 0) {
    close(native_pid_fd);
    if (setpgid(0, 0) == -1) {
      perror("set harness process group");
      _exit(125);
    }
    execvp(argv[3], argv + 3);
    perror("exec harness");
    _exit(127);
  }
  if (setpgid(child, child) == -1 && errno != EACCES && errno != ESRCH) {
    perror("set harness child process group");
    kill(child, SIGKILL);
    waitpid(child, NULL, 0);
    return 125;
  }
  if (dprintf(native_pid_fd, "%ld\n", (long)child) < 0) {
    perror("report harness pid");
    kill(-child, SIGKILL);
    waitpid(child, NULL, 0);
    return 125;
  }
  close(native_pid_fd);

  int stop_sent = 0;
  if (stop_signal != 0) {
    // A parent-death signal can arrive after the child starts but before the first waitpid.
    // Consume it here rather than entering a wait that no later signal will interrupt.
    stop_signal = 0;
    stop_sent = 1;
    alarm(5);
    if (signal_group(child, SIGTERM, "stop harness process group") == -1) {
      return 125;
    }
  }
  int status;
  while (waitpid(child, &status, 0) == -1) {
    if (errno != EINTR) {
      perror("wait for harness");
      return 125;
    }
    if (stop_signal == 0) {
      continue;
    }
    // On runner death the harness's stdin is already closed. Give it the same SIGTERM it gets
    // during orderly shutdown so it can persist its native resume state; this supervisor retains
    // the state-owner descriptor until that child group actually exits. A replacement therefore
    // fails closed rather than overlapping a native process that has not stopped yet.
    if (stop_signal == SIGALRM) {
      stop_signal = 0;
      alarm(0);
      if (signal_group(child, SIGKILL, "force-stop harness process group") == -1) {
        return 125;
      }
      continue;
    }
    if (!stop_sent) {
      // On runner death the harness's stdin is already closed. Give it the same SIGTERM it gets
      // during orderly shutdown so it can persist its native resume state. If it cannot exit,
      // the kernel timer force-fences the entire process group rather than letting a successor
      // wait forever with no writer available.
      stop_signal = 0;
      stop_sent = 1;
      alarm(5);
      if (signal_group(child, SIGTERM, "stop harness process group") == -1) {
        return 125;
      }
    }
  }
  alarm(0);
  if (!stop_sent && stop_signal != 0) {
    // The leader can exit at the same instant its runner dies. Its descendants still need the
    // parent-death fence even though there is no leader left to wait for.
    stop_signal = 0;
    stop_sent = 1;
    if (signal_group(child, SIGTERM, "stop remaining harness process group") == -1) {
      return 125;
    }
  }
  if (stop_sent && kill(-child, 0) == 0) {
    // The leader may have exited while a tool ignored its graceful stop. Do not release the
    // inherited ownership descriptor until that remaining group is force-fenced.
    if (signal_group(child, SIGKILL, "force-stop remaining harness process group") == -1) {
      return 125;
    }
  } else if (stop_sent && errno != ESRCH) {
    perror("check remaining harness process group");
    return 125;
  }
  if (WIFEXITED(status)) {
    return WEXITSTATUS(status);
  }
  if (WIFSIGNALED(status)) {
    return 128 + WTERMSIG(status);
  }
  return 125;
}
