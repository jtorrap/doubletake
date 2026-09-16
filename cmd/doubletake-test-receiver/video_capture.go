package main

import (
	"encoding/binary"
	"fmt"
	"os"
	"sync"
	"time"

	"doubletake/internal/airplay"
)

// This format is only consumed by the synthetic CI fixture. Each record is
// kind:u8, wire PTS:u64, monotonic arrival nanoseconds:u64, size:u32, payload.
// Payloads are plaintext AVCC or codec configuration, never live user media.
const receiverVideoCaptureMagic = "DTVID001"
const receiverVideoCaptureLimit = 256 * 1024 * 1024

type receiverVideoCapture struct {
	mu    sync.Mutex
	file  *os.File
	start time.Time
	bytes int
}

func newReceiverVideoCapture(path string) (*receiverVideoCapture, error) {
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return nil, err
	}
	if _, err := file.WriteString(receiverVideoCaptureMagic); err != nil {
		file.Close()
		return nil, err
	}
	return &receiverVideoCapture{file: file, start: time.Now(), bytes: len(receiverVideoCaptureMagic)}, nil
}

func (c *receiverVideoCapture) Observe(packet airplay.ReceiverVideoPacket) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.bytes+21+len(packet.Payload) > receiverVideoCaptureLimit {
		return fmt.Errorf("synthetic video capture exceeded its size bound")
	}
	var header [21]byte
	header[0] = packet.Type
	binary.BigEndian.PutUint64(header[1:9], packet.Timestamp)
	binary.BigEndian.PutUint64(header[9:17], uint64(packet.ReceivedAt.Sub(c.start).Nanoseconds()))
	binary.BigEndian.PutUint32(header[17:21], uint32(len(packet.Payload)))
	if _, err := c.file.Write(header[:]); err != nil {
		return err
	}
	if _, err := c.file.Write(packet.Payload); err != nil {
		return err
	}
	c.bytes += len(header) + len(packet.Payload)
	return nil
}

func (c *receiverVideoCapture) Close() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.file.Close()
}
