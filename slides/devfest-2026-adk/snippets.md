

```
# With string identifiers for nodes
workflow_graph = {
    "edges": [
        (
            "START",
            "intake",
            "data_collection",
            "investigation_phase",
            "investigation_route",
        ),
        (
            "investigation_route",
            {
                "continue": "investigation_phase",
                "done": "change_correlation",
            },
        ),
        (
            "change_correlation",
            "synthesis",
            "verifier",
            "verifier_route",
        ),
        (
            "verifier_route",
            {
                "repair": "synthesis",
                "done": "pipeline_done",
            },
        ),
    ]
}
```


and
```
# With node function / object references
workflow_graph = {
    "edges": [
        ("START", intake, data_collection, investigation_phase, investigation_route),
        (investigation_route, {"continue": investigation_phase, "done": change_correlation}),
        (change_correlation, synthesis, verifier, verifier_route),
        (verifier_route, {"repair": synthesis, "done": pipeline_done}),
    ]
}
```
