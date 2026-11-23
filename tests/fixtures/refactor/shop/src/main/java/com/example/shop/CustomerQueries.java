package com.example.shop;

import java.util.HashMap;
import java.util.Map;

/** Uses of Customer#code by name: a path through a relation, the type's alias, another
 * entity's field of the same name, a map key, a property id. */
public class CustomerQueries {
    public String byPayer() {
        return "payer.code == :p";
    }

    public String byAlias() {
        return "customer.code.startsWith(:p)";
    }

    public String otherEntity() {
        return "product.code == :p";
    }

    public Map<String, Object> params(String v) {
        Map<String, Object> m = new HashMap<>();
        m.put("code", v);
        return m;
    }

    public String property() {
        return bind("code");
    }

    private String bind(String property) {
        return property;
    }
}
