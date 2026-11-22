package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/TechnoTUT/moip-toolkit/server/internal/model"
)

func TestNodeInfo(t *testing.T) {
	want := model.NodeInfo{ID: "utone-a1b2c3", Hostname: "utone-a1b2c3", Version: "v1", Roles: []string{"rx", "tx"}}
	srv := httptest.NewServer((&Server{Info: want}).Handler())
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/api/node/info")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var got model.NodeInfo
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 200 || got.ID != want.ID || got.Version != want.Version || len(got.Roles) != 2 {
		t.Fatalf("status=%d got %+v", resp.StatusCode, got)
	}
}
