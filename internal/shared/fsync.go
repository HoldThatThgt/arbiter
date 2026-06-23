package shared

import "os"

// SyncDir best-effort fsyncs a directory so a preceding rename/create of an
// entry within it survives a crash (POSIX rename is not durable until the
// containing dir is fsync'd). Errors are ignored: durability is best-effort
// and not all filesystems permit syncing a directory handle.
func SyncDir(dir string) {
	d, err := os.Open(dir)
	if err != nil {
		return
	}
	_ = d.Sync()
	_ = d.Close()
}
