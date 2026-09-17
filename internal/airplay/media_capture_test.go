package airplay

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"path/filepath"
	"testing"
	"time"
)

func mediaFixture(t *testing.T, payload []byte) (string, <-chan map[string]any) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "media.sock")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	requests := make(chan map[string]any, 1)
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		var request map[string]any
		if json.NewDecoder(conn).Decode(&request) != nil {
			return
		}
		requests <- request
		conn.Write(append([]byte{1}, payload...))
		io.Copy(io.Discard, conn)
	}()
	return path, requests
}

func TestMediaVideoRetainsSourcePTSAndCancellation(t *testing.T) {
	pts := time.Now().Add(-50 * time.Millisecond)
	path, requests := mediaFixture(t, joinTestRTPPackets(testRTPPacket{1, 9000, ntpFromTime(pts), true, []byte{0x65, 0x80}}))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	capture, err := StartMediaVideo(ctx, path, 1280, 720)
	if err != nil {
		t.Fatal(err)
	}
	defer capture.Stop()
	request := <-requests
	if request["kind"] != "video" || request["width"] != float64(1280) || request["height"] != float64(720) {
		t.Fatal(request)
	}
	frame, err := capture.ReadVideoAccessUnit()
	if err != nil || frame.PTS.Sub(pts).Abs() > time.Microsecond {
		t.Fatalf("PTS changed: %v %v", frame.PTS, err)
	}
	done := make(chan error, 1)
	go func() { _, err := capture.ReadVideoAccessUnit(); done <- err }()
	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("cancel returned media")
		}
	case <-time.After(time.Second):
		t.Fatal("read survived cancellation")
	}
}

func TestMediaAudioRetainsSampleClock(t *testing.T) {
	pts := time.Now().Add(-30 * time.Millisecond)
	pcm, _ := testL16Samples(704, 0x1000)
	path, requests := mediaFixture(t, testFramedL16Packet(1, 800, pts, pcm))
	capture, err := StartMediaAudio(context.Background(), path, AudioCodecALAC)
	if err != nil {
		t.Fatal(err)
	}
	defer capture.Stop()
	if request := <-requests; request["kind"] != "audio" {
		t.Fatal(request)
	}
	buffer := make([]byte, 8192)
	_, first, err := capture.readFramePosition(buffer)
	if err != nil || first.PTS.Sub(pts).Abs() > time.Microsecond || !first.HasSourceRTP {
		t.Fatalf("first frame: %+v %v", first, err)
	}
	_, second, err := capture.readFramePosition(buffer)
	if err != nil || second.SourceRTP-first.SourceRTP != 352 {
		t.Fatalf("sample clock changed: %+v %v", second, err)
	}
}
