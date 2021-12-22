#define _GNU_SOURCE

#include <dlfcn.h>
#include <linux/fcntl.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/syscall.h>
#include <unistd.h>

static int (*real_pause)(void);
static int (*real_mount)(const char *source, const char *target,
			 const char *filesystemtype, unsigned long mountflags,
			 const void *data);
static long (*real_syscall)(long number, ...);

static int exefd;
static int sock;

__attribute__((constructor))
static void init(void)
{
	char *sock_fd_env;
	char *exe_fd_env;

	real_pause = dlsym(RTLD_NEXT, "pause");
	if (!real_pause)
		exit(1);
	real_mount = dlsym(RTLD_NEXT, "mount");
	if (!real_mount)
		exit(1);
	real_syscall = dlsym(RTLD_NEXT, "syscall");
	if (!real_syscall)
		exit(1);

	sock_fd_env = getenv("SOCK_FD");
	if (!sock_fd_env)
		exit(1);

	sock = atoi(sock_fd_env);

	exe_fd_env = getenv("EXE_FD");
	if (!exe_fd_env)
		exit(1);

	exefd = atoi(exe_fd_env);
}

int pause(void)
{
	static atomic_flag closed;

	if (!atomic_flag_test_and_set(&closed)) {
		close(sock);
		close(exefd);
	}

	return real_pause();
}

int mount(const char *source, const char *target,
	  const char *filesystemtype, unsigned long mountflags,
	  const void *data)
{
	if (source && !strcmp(source, "/dev/discord") &&
	    target && !strcmp(target, "/dev/discord") &&
	    !filesystemtype && mountflags & MS_REMOUNT)
		return 0;

	return real_mount(source, target, filesystemtype, mountflags, data);
}

static int real_execveat(int dirfd, const char *pathname,
			 char *const argv[], char *const envp[],
			 int flags)
{
	return real_syscall(SYS_execveat, dirfd, pathname, argv, envp, flags);
}

int execveat(int dirfd, const char *pathname,
	     char *const argv[], char *const envp[],
	     int flags)
{
	if (strncmp(argv[0], "/proc/self/fd/", strlen("/proc/self/fd/"))) {
		return real_execveat(dirfd, pathname, argv, envp, flags);
	}

	int n_argv;
	for (n_argv = 0; argv[n_argv]; n_argv++);

	char *new_argv[n_argv + 1];
	new_argv[0] = "-bash";

	for (int i = 1; i <= n_argv; i++) {
		new_argv[i] = argv[i];
	}

	return real_execveat(dirfd, pathname, new_argv, envp, flags);
}

int execve(const char *pathname, char *const argv[],
	   char *const envp[])
{
	return execveat(AT_FDCWD, pathname, argv, envp, 0);
}

int execv(const char *pathname, char *const argv[])
{
	return execve(pathname, argv, environ);
}

long syscall(long number, ...)
{
	long a1, a2, a3, a4, a5, a6;
	va_list ap;

	va_start(ap, number);
	a1 = va_arg(ap, long);
	a2 = va_arg(ap, long);
	a3 = va_arg(ap, long);
	a4 = va_arg(ap, long);
	a5 = va_arg(ap, long);
	a6 = va_arg(ap, long);
	va_end(ap);

	if (number == SYS_execve)
		return execve((void *)a1, (void *)a2, (void *)a3);
	if (number == SYS_execveat)
		return execveat(a1, (void *)a2, (void *)a3, (void *)a4, a5);

	return real_syscall(number, a1, a2, a3, a4, a5, a6);
}
