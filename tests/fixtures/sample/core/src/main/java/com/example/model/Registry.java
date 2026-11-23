package com.example.model;

import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/** Holds a map, and two fields that are not maps. */
public class Registry {
    private final Map<String, Account> byId = new ConcurrentHashMap<>();
    private final Set<String> names = ConcurrentHashMap.newKeySet();
    private final List<Map.Entry<String, String>> entries = List.of();

    public int size() {
        return byId.size() + names.size() + entries.size();
    }
}
