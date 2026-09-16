package main

import (
	"bytes"
	"encoding/binary"
	"os"
	"path/filepath"
	"testing"
	"time"

	"doubletake/internal/airplay"
)

func TestReceiverVideoCaptureIsExplicitPrivateAndPreservesTiming(t *testing.T) {
	path := filepath.Join(t.TempDir(), "synthetic-video.bin")
	capture, err := newReceiverVideoCapture(path)
	if err != nil {
		t.Fatal(err)
	}
	if duplicate, err := newReceiverVideoCapture(path); err == nil {
		duplicate.Close()
		t.Fatal("existing capture was overwritten")
	}
	payload := []byte{1, 2, 3}
	if err := capture.Observe(airplay.ReceiverVideoPacket{
		Type: 1, Timestamp: 42, ReceivedAt: capture.start.Add(33 * time.Millisecond), Payload: payload,
	}); err != nil {
		t.Fatal(err)
	}
	if err := capture.Close(); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(data[:8]) != receiverVideoCaptureMagic || data[8] != 1 ||
		binary.BigEndian.Uint64(data[9:17]) != 42 ||
		binary.BigEndian.Uint64(data[17:25]) != uint64(33*time.Millisecond) ||
		binary.BigEndian.Uint32(data[25:29]) != 3 || !bytes.Equal(data[29:], payload) {
		t.Fatalf("wrong capture record: %x", data)
	}
	info, err := os.Stat(path)
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatalf("capture permissions: %v %v", info, err)
	}
}
