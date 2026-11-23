package com.example.shop;

import java.util.List;
import javax.jdo.annotations.Column;
import javax.jdo.annotations.Convert;
import javax.jdo.annotations.Element;
import javax.jdo.annotations.Extension;
import javax.jdo.annotations.Join;
import javax.jdo.annotations.PersistenceCapable;
import javax.jdo.annotations.Persistent;
import javax.jdo.annotations.Unique;
import javax.jdo.annotations.Version;
import javax.jdo.annotations.VersionStrategy;

/** A version kept in a field, a join table, the inverse side of a relation, a form. */
@PersistenceCapable(table = "CUSTOMERS")
@Version(strategy = VersionStrategy.VERSION_NUMBER, column = "VERSION",
        extensions = {@Extension(vendorName = "datanucleus", key = "field-name", value = "version")})
@Unique(name = "CUSTOMER_CODE", members = {"code"})
@Form(fields = "code,nickname")
public class Customer implements Buyer {
    @Column(name = "CODE", length = 20, allowsNull = "false")
    private String code;

    @Convert(Upper.class)
    private String nickname;

    private long version;

    @Persistent(table = "CUSTOMER_ORDERS")
    @Join(column = "CUSTOMER_ID")
    @Element(column = "ORDER_ID")
    private List<Order> history;

    @Persistent(mappedBy = "payer")
    private List<Payment> payments;

    public String getCode() {
        return code;
    }

    public void setCode(String code) {
        this.code = code;
    }
}
