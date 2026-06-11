---
title: On Protocol Buffers
---

In the end of February, I'm finishing my 6 month internship at Google Paris.
Google is very big on engineering. If you need to perform some task, it is
very likely the task has been solved before. In fact, your problem usually
has about 6 solutions in the codebase, and by Murphy's law, 5 of them don't
fit your requirements in subtle ways and the sixth is deprecated. Its sole
maintainer left Google back when Internet Explorer was a real browser.
Have fun writing solution number seven.

On the other hand, Google employs heroes and the basic infrastructure
is full of masterpieces of engineering. Today, I want to talk about
[Protocol Buffers](https://developers.google.com/protocol-buffers/).

Protocol Buffers (also _protobufs_ or just _protos_) are a storage format
conceptually similar to JSON, YAML or XML.
Every protocol buffer has a type called a "message". Message definitions
live in `.proto` files:

```
package goliath.monetization.surveillance;

message Person {
  optional int32 id = 1;
  required string name = 2;
  optional string occupation = 3;

  enum Country {
    USA = 1;
    CANADA = 2;
    BRAZIL = 3;
    EUROPE = 4;
  }

  message Address {
    optional string city = 1;
    optional Country country = 2;
  }
  repeated Address address = 4;
}
```

Protocol buffers are everywhere. The packets Google's RPC framework sends
contain protocol buffers. Protocol buffers are commonly used as library API
types. Google databases (e.g. BigTable, SSTable) are usually designed to store
byte-strings and protocol buffers are the byte-string representation
of structured data.

The "default" serialization format of protocol buffers is binary.
It's basically a list of `(tag, value)` pairs. A field declaration like
`optional Address address = 3;` means that the `address` field will use
the tag `3`.

Protocol buffers also have a nice human-readable text format, which looks
a little bit like JSON. For example, a
`goliath.monetization.surveillance.Person` proto can be represented as:

```
id: 42
name: "Steve Jobs"
address { city: "Cupertino" }
address { country: USA }
```

You can use protocol buffers from a wide variety of programming languages, but
the main Google languages (C++, Java, Python and Go) have best support.
For any given language, the `protoc` compiler munches `.proto` files and
generates code in your language that lets you manipulate the messages.

C++ code using protocol buffers might look like this
(adapted from an [official example](https://code.google.com/p/protobuf/)):

```cpp
#include "goliath/monetization/surveillance/person.pb.h"

Person person;
person.set_id(123);
person.set_name("Bob");
Person::Address* address = person.add_address();
address->set_country(Person::Country::EUROPE);

// Store the person in the binary format.
fstream out("person.pb", ios::out | ios::binary | ios::trunc);
person.SerializeToOstream(&out);
out.close();

// And much later...

Person person;
fstream in("person.pb", ios::in | ios::binary);
if (!person.ParseFromIstream(&in)) {
  cerr << "Failed to parse person.pb." << endl;
  exit(1);
}

cout << "ID: " << person.id() << endl;
cout << "name: " << person.name() << endl;
for (const Person::Address& address : person.address()) {
  if (address.has_city()) {
    cout << "city: " << address.city() << endl;
  }
}
```

<h2>Why I love Protocol Buffers</h2>

<h3>Protocol Buffers are language-agnostic</h3>
You can read, write and manipulate Protocol Buffers in many popular languages.
You can do the same thing with JSON, YAML or XML, but your libraries
will differ between languages. You may need to translate from a library
representation of the serialization format into something you can easily
manipulate. For example, your XML parser might not let you add nodes to
a document.

On the other hand, Protocol Buffers generated code provides the same functions
across all languages. Whenever it makes sense, the same concepts have
the same name. Reading a `name` is not
`string name = XmlGetContent(XmlFindTag(dom, "name"));` in C++ and
`name = dom.find('//name').content` in Ruby. It's `string name = person.name();`
in C++, `name = person.name` in Ruby or Python, and so on.

<h3>Protocol Buffers have a schema and are reasonably type-safe</h3>
Your language will help you write correct code. You cannot possibly assign
anything to an undefined field by mistake. The generated code will not let you
do this.

In contrast, JSON or YAML are basically totally untyped dictionaries.
It's very easy to forget a nesting level in an expression like
`response["address"]["city"]` (the address is actually an array, oops).
You can write run-time checkers, but you will need to write them again
for every language your code communicates with.

<h3>Protocol Buffers are pure data</h3>
If your Ruby app receives a "`Person`" from a JSON service, it will probably
end up parsing the JSON into its own Ruby POD representation of a `Person`.

With protocol buffers, you automatically get this POD representation.

The generated code cannot be extended (unless you try really hard).
This forces you to separate business logic from data storage, which is
a good thing.

<h3>Protocol Buffers are compact and fast</h3>
The binary serialization format is compact - for example, numbers and enums are
stored in binary and field names are not stored in the protocol buffer.
Remember, we have integer tags.

Protocol buffers are also fast. In C++, every message is represented as
a separate class and encoding and decoding can be optimized by the compiler.
No reflection is needed when manipulating protocol buffers.
(However, you still have a descriptor of every compiled-in protocol buffer
available at runtime.)

<h3>Protocol Buffers are language-agnostic API definitions as code</h3>
The API of your library or binary can be defined as a `.proto` file.
Once you specify some way of communicating with your code (for example
HTTP, Unix stdin, etc.), you can define the details of your API
in a single `.proto` file.

Your users are in no way bound to your choice of language -- they just
use generated code for their language. You can also _change your entire
implementation_, including the language, while keeping the same `.proto`
API definition. This is especially useful in large systems --
machine-readable API definitions let your computer help you maintain services
and clients compatible.

You can include `proto` files, so you can reuse common API elements.

<h3>Protocol Buffers can be future-proof</h3>
You can freely change names of protocol buffer fields and messages.
Any binary protocol buffers you may have in your big database will still
be readable - as long as tag values keep referring to the same things.

As shown in the `.proto` example above, protocol buffer fields must be
either `optional`, or `required`. Marking a field as `required` affects
the storage format of your message.

`optional` fields are optional. If we add a `optional string street = 3;`
into `Address`, all `Address` messages created by previously existing code
will still be readable.

If a serialized protocol buffer refers to a tag number not declared in
the `.proto`, it's not an error. Therefore, old binaries will be capable of
running on addresses with streets, even if the `street` field was
not present in the `.proto` at compilation time.

<h3>Embracing change</h3>
We can gracefully add and remove optional fields as messages are repurposed.
Let's make the decision that we will be tracking `Person` occupations
in a shiny new database called `JobStore`, so the `Person.occupation` field
now stores duplicate information.

To keep our code DRY, we want to remove `Person.occupation`.

First, we will add a comment for humans to make sure every user is informed
that they shouldn't write new code depending on the field we are deprecating:

```
message Person {
  ...
  // Deprecated - occupations are now stored in JobStore.
  optional string occupation = 3 [deprecated=true];
  ...
}
```

The next step is to change any readers of the `occupation` field to instead
get the occupation from `JobStore`:

```cpp
string IsPlumber(const Person& person) {
  return person->occupation() == "plumber";
}

// Becomes:
string IsPlumber(const Person& person) {
  return JobStore::FindOccupation(person.id) == JobStore::PLUMBER;
}
```

After we kill all readers of the field, we can start removing writers
of the field:

```cpp
void LoadBob(Person* person) {
  person->set_id(15);
  person->set_name("Bob");
  // person->set_occupation("chief plumbing officer");
}
```

When there are no more references to `occupation` in code, we can remove
the field from the `.proto`. We need to keep a comment that
no future fields can use the tag number `3`. If a future field accidentally
uses this tag number for a different purpose, we may not get an error -
any strings in tag number `3` will be interpreted as the new field,
because protocol buffers do not store any type information aside from tag
numbers.

A tombstone left by a dead field may look like this:

```
message Person {
  optional int32 id = 1;
  required string name = 2;
  // 3 is deprecated.

  enum Country { ... }
  ...
}
```

Note that this procedure would not work if the `occupation` field was
initially marked as `required`, because `required` changes the binary
serialization format. You would need to stop using the `Person` message
altogether to get rid of the no longer required field.

When we wish to add a new field to a message, we can just add it as
optional (or repeated) and make new code deal with cases when this new
field is missing.

This "future safety" is an important feature of protocol buffers.
In a Google-scale codebase, some protocol buffers may be used
by hundreds of users across multiple products. Because you can gradually
add or remove support for fields, changes can be performed in small,
manageable chunks. There is no 1200-line "cleanup commit" that changes
1000 files everywhere from AdWords to YouTube - every affected piece of code
can be adjusted in an isolated change.

For further reading about Protocol Buffers, you can have a look at the
[documentation](https://developers.google.com/protocol-buffers/) or
at the GitHub source at [google/protobuf](https://github.com/google/protobuf/).
Wikipedia also has a [neat article](http://en.wikipedia.org/wiki/Protocol_Buffers).
