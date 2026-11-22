package avahi

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

type fakeSession struct {
	mu         sync.Mutex
	registered []string
	closed     bool
	events     chan EventKind
	altErr     error
	regErr     error
	regErrs    []error // returned by successive Register calls before regErr applies
}

func newFake() *fakeSession { return &fakeSession{events: make(chan EventKind, 4)} }

func (f *fakeSession) Register(name string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.regErrs) > 0 {
		err := f.regErrs[0]
		f.regErrs = f.regErrs[1:]
		if err != nil {
			return err
		}
	} else if f.regErr != nil {
		return f.regErr
	}
	f.registered = append(f.registered, name)
	return nil
}
func (f *fakeSession) AltName(name string) (string, error) { return name + "-2", f.altErr }
func (f *fakeSession) Events() <-chan EventKind            { return f.events }
func (f *fakeSession) Close() error                        { f.mu.Lock(); f.closed = true; f.mu.Unlock(); return nil }
func (f *fakeSession) names() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.registered...)
}
func (f *fakeSession) isClosed() bool { f.mu.Lock(); defer f.mu.Unlock(); return f.closed }

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	for end := time.Now().Add(2 * time.Second); time.Now().Before(end); time.Sleep(2 * time.Millisecond) {
		if cond() {
			return
		}
	}
	t.Fatalf("timed out waiting for %s", what)
}

// run starts an Advertiser whose Dial hands out the given results in order.
func run(t *testing.T, dials ...func() (Session, error)) (cancel func(), dialed func() int) {
	t.Helper()
	var mu sync.Mutex
	n := 0
	a := &Advertiser{
		Service:  Service{Name: "utone-a", Type: "_moip._tcp", Port: 8000},
		RetryMin: time.Millisecond, RetryMax: 5 * time.Millisecond,
		Dial: func() (Session, error) {
			mu.Lock()
			defer mu.Unlock()
			i := min(n, len(dials)-1)
			n++
			return dials[i]()
		},
	}
	ctx, c := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { a.Run(ctx); close(done) }()
	t.Cleanup(func() { c(); <-done })
	return c, func() int { mu.Lock(); defer mu.Unlock(); return n }
}

func ok(s *fakeSession) func() (Session, error) { return func() (Session, error) { return s, nil } }

func TestRegistersAndWithdrawsOnShutdown(t *testing.T) {
	f := newFake()
	cancel, _ := run(t, ok(f))
	waitFor(t, "registration", func() bool { return len(f.names()) == 1 })
	if f.names()[0] != "utone-a" {
		t.Fatalf("got %v", f.names())
	}
	cancel()
	waitFor(t, "session closed", f.isClosed)
}

func TestCollisionRegistersUnderAlternativeName(t *testing.T) {
	f := newFake()
	run(t, ok(f))
	waitFor(t, "first registration", func() bool { return len(f.names()) == 1 })
	f.events <- Collision
	waitFor(t, "re-registration", func() bool { return len(f.names()) == 2 })
	if got := f.names(); got[1] != "utone-a-2" {
		t.Fatalf("got %v", got)
	}
	f.events <- Collision // a second collision continues from the new name
	waitFor(t, "third registration", func() bool { return len(f.names()) == 3 })
	if got := f.names(); got[2] != "utone-a-2-2" {
		t.Fatalf("got %v", got)
	}
}

func TestReconnectsWhenAvahiIsNotUpYetOrGoesAway(t *testing.T) {
	first, second := newFake(), newFake()
	_, dialed := run(t,
		func() (Session, error) { return nil, errors.New("avahi-daemon is not running") },
		ok(first),
		ok(second),
	)
	waitFor(t, "registration after a failed dial", func() bool { return len(first.names()) == 1 })
	first.events <- Gone // avahi-daemon restarted
	waitFor(t, "registration on a new session", func() bool { return len(second.names()) == 1 })
	if !first.isClosed() {
		t.Fatal("the old session must be closed")
	}
	if dialed() != 3 {
		t.Fatalf("dialed %d times", dialed())
	}
	if second.names()[0] != "utone-a" {
		t.Fatalf("a new session starts again from the configured name, got %v", second.names())
	}
}

func TestFailureAndRegisterErrorsAreRetried(t *testing.T) {
	bad := newFake()
	bad.regErr = errors.New("AddService: boom")
	good := newFake()
	run(t, ok(bad), ok(good))
	waitFor(t, "registration after a register error", func() bool { return len(good.names()) == 1 })
	if !bad.isClosed() {
		t.Fatal("the failed session must be closed")
	}
}

func TestNameInUseOnThisHostIsRegisteredUnderAnAlternative(t *testing.T) {
	f := newFake()
	f.regErrs = []error{ErrCollision, ErrCollision} // taken twice, then free
	run(t, ok(f))
	waitFor(t, "registration", func() bool { return len(f.names()) == 1 })
	if got := f.names()[0]; got != "utone-a-2-2" {
		t.Fatalf("got %q", got)
	}
}

func TestGivesUpAfterTooManyCollisionsAndRetriesLater(t *testing.T) {
	bad, good := newFake(), newFake()
	bad.regErr = ErrCollision
	run(t, ok(bad), ok(good))
	waitFor(t, "a fresh session after giving up", func() bool { return len(good.names()) == 1 })
	if !bad.isClosed() {
		t.Fatal("the stuck session must be closed")
	}
}
