package upstream

import (
	"context"
	"encoding/json"
	"fmt"
)

// alertmanagerSilence contains the ownership fields returned by getSilences.
// The shape follows Alertmanager's v2 API; missing or malformed ownership
// data causes SilenceCreatedBy to return an error so authorization fails
// closed.
type alertmanagerSilence struct {
	ID        string `json:"id"`
	CreatedBy string `json:"createdBy"`
}

// SilenceCreatedBy resolves a silence to its creator with one getSilences
// call and filters the result by ID. getSilencesTool must name the lookup tool
// on the same backend as the original request because the shim fronts multiple
// Alertmanager instances.
//
// Returns an error on any lookup failure (timeout, non-200, malformed
// result, tool-reported error, the silence not appearing in the list, or a
// found silence with no resolvable createdBy) so the caller can fail closed.
//
// This is the one lookup that opts OUT of the shared result cache (see
// cache.go). Its arguments are always empty, so every silence in the cluster
// shares a single cache key, and its result is a LIST that grows: a silence
// created after the entry was stored is absent from the cached copy, and
// "absent" here means fail closed. Caching would turn deleting a
// just-created silence into a denial for the rest of the TTL.
func SilenceCreatedBy(ctx context.Context, getSilencesTool string, client ToolCaller, silenceID string) (string, error) {
	result, err := Uncached(client).CallTool(ctx, getSilencesTool, map[string]any{})
	if err != nil {
		return "", fmt.Errorf("alertmanager silence owner lookup for %q: %w", silenceID, err)
	}
	var silences []alertmanagerSilence
	if err := json.Unmarshal([]byte(result.Text()), &silences); err != nil {
		return "", fmt.Errorf("alertmanager silence owner lookup for %q: malformed getSilences result: %w", silenceID, err)
	}
	for _, sil := range silences {
		if sil.ID != silenceID {
			continue
		}
		if sil.CreatedBy == "" {
			return "", fmt.Errorf("alertmanager silence owner lookup for %q: getSilences result has no resolvable createdBy", silenceID)
		}
		return sil.CreatedBy, nil
	}
	return "", fmt.Errorf("alertmanager silence owner lookup for %q: not found in getSilences result", silenceID)
}
