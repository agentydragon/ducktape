#include <stdio.h>
#include <signal.h>
#include <sys/types.h>
#include <unistd.h>

/* A native-harness stand-in for verifying inherited state ownership across a leader exit. */
int main(void) {
  pid_t child = fork();
  if (child < 0) {
    perror("fork background harness child");
    return 125;
  }
  if (child == 0) {
    signal(SIGTERM, SIG_IGN);
    for (;;) {
      pause();
    }
  }
  printf("%ld\n", (long)child);
  fflush(stdout);
  for (;;) {
    pause();
  }
}
